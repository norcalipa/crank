<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Moderated usability validation (#492)

**Issue:** [#492](https://github.com/norcalipa/crank/issues/492)
**Status:** Protocol only — no session has been run

This is the kit for a five-person moderated usability round on the contextual
assistant workspace, and the empty record its results go into. It is an
evidence record format, not a claim: until the
[results record](#results-record) is filled in for a specific release SHA,
nothing here says the product passed. The staged release decisions that use
the round's outcome are in `docs/rollout-gates.md`, "Contextual assistant
staged release (#492)".

Merging this document does not complete #492. The sessions, the scoring, the
release decisions and the sign-offs are human work, listed under
[What only the owner can do](#what-only-the-owner-can-do).

## Decisions pending owner confirmation

Each line is a default this kit and the release section were written to. Each
is marked **default — owner may change**; changing a decision means editing
its line here (and, where a line says so, the one value it names).

| # | Decision | Default — owner may change |
|---|---|---|
| D1 | Shell rollback | **default — owner may change:** redeploying the previous image, followed by a revert commit on `main`, is accepted as the shell's rollback; `assistant_shell` stays `planned` and no shell switch is built. The image rollback alone is undone by the next merge to `main`, so no other pull request is merged until the revert has deployed (`docs/rollout-gates.md`, "The shell's rollback is not durable"). |
| D2 | Gate thresholds | **default — owner may change:** the one existing #328 stage number (reply success `0.90`) is reused as `provisional`; four more `provisional` numbers are new in #492 (`docs/rollout-gates.md`, "Provisional numbers for #492 gates"); every other new gate is `baseline_required` until 14 days of data exist with the capability enabled; no new alerts. Values live in `release_gates:` in `docs/monitoring.yaml`. No gate has a sample floor yet (see "Sample floors and alert policies to lock" below). |
| D3 | Where sessions run | **default — owner may change:** a staging instance (`ENV=staging`, orchestrator provider, at least one real approved source). Documented alternative: production as an internal canary with throwaway accounts, after durable enablement — which is not possible until #555 is fixed (see Preconditions). |
| D4 | Pass bar for A2 | **default — owner may change:** at least 4 of 5 participants answer all four comprehension probes (C1–C4) correctly. |
| D5 | Sign-offs and where evidence lives | **default — owner may change:** the repository owner signs each role by GitHub handle and date; results are recorded by an owner-authored docs pull request that fills the results record; raw notes never enter the repository. This is the only sign-off mechanism: an approving review is not one. |
| D6 | How long raw notes and recordings are kept | **default — owner may change:** the raw notes, recordings and transcripts, the participant-number mapping, the recruiting messages and the calendar invitations are destroyed when the results record is merged, and in any case no later than 30 days after the last session — also if the round is abandoned or repeated. |
| D7 | Where decision records live and what they may show | **default — owner may change:** each phase decision record is the body of its pull request, which is public. Aggregate `data_counts` and gate values are accepted as public; nothing that identifies a person or quotes typed text may appear. Alternative: keep the record outside the repository and put only the decision and the sign-off in the pull request. |

### Sample floors and alert policies to lock

These have **no default**. Each release gate in `release_gates:`
(`docs/monitoring.yaml`) needs a sample floor N before it can pass: below N
events in its window the outcome is hold. Until the owner supplies N the gate
holds whatever its query returns, so no gate can pass today. The three
`alerts_quiet` gates also need the owner to confirm that the named alerts
exist in the alerting tool, because nothing in the repository creates them.
"Exist" means more than a name: the alert's query, threshold and window equal
its entry under `alerts:` in `docs/monitoring.yaml`, and it has been seen to
open once. Rules: `docs/monitoring.md`, "Release decision gates".

**Open: does `deadline-resource-pressure` ever open?** It is
`sum(deadline_reached)` over `matching_batch`, and `deadline_reached` is sent
as a boolean. NRQL does not coerce types, and this cannot be tested from the
repository. The owner decides, when checking the alert policy for
`matching-alerts-quiet`: if the alerting tool sums it to a non-zero number when
a drain stops at its deadline, nothing changes; if it does not, the alert needs
a numeric attribute or a `count` of `deadline_reached = true`, which is a change
to `alerts:` and belongs in the gate-locking pull request, not here. Until
then `matching-alerts-quiet` is not evidence about deadline pressure.

**A floor counts requests and events, not people.** Telemetry carries no user
or session dimension, and the availability endpoints send one event per
request with no throttle: one signed-in account polling in a loop, or one
open tab, can supply a whole floor, and sign-up is open. The five moderated
sessions and the owner's own smoke tests fall in the same windows and can be
most of a sample; nothing excludes them. An outsider can pad a sample, and can
lower a success share (an apply with a stale token counts as a failed apply).
Choose N knowing it is a request count. `assistant-ready-share` counts only
`cached = false` events: the classification is cached for every signed-in user
at once, so that is at most one event per cache period however many clients
poll.

| Gate | What the floor counts | Window | Floor N | Alert policy |
|---|---|---|---|---|
| `priorities-apply-success` | `preference_decision` events with `decision = 'apply'` | 14 days | not set | — |
| `interactive-reply-success` | `assistant_turn` events with `phase = 'attempted'` | 7 days | not set | — |
| `interactive-alerts-quiet` | `interactive_call` events with `status = 'provider_succeeded'` (live signal; error events do not count) | 7 days | not set | not confirmed |
| `interactive-time-to-first-result` | `assistant_first_result` events | 14 days | not set | — |
| `job-source-alerts-quiet` | `inventory_health` events that carry the gauges (live signal; a degraded probe event does not count) | 7 days | not set | not confirmed |
| `job-matches-source-unavailable` | `availability_state` events for `job_matches`, every state (a request count; one account can supply it) | 7 days | not set | — |
| `assistant-ready-share` | `availability_state` events for `assistant_status`, signed-out excluded, `cached = false` only (one per cache period) | 7 days | not set | — |
| `publication-outbox-age` | items a completed `publication_sweep` processed (the sum of `processed`): an empty outbox with the consumer off reads age 0 on every probe tick, so the probe's events are not the sample | 24 hours | not set | — |
| `publication-to-match-lag` | `matching_batch` events from the `match_recompute` stage with a measured lag | 14 days | not set | — |
| `matching-alerts-quiet` | `matching_batch` events from the `match_recompute` stage (live signal) | 7 days | not set | not confirmed |
| `evidence-stale-share` | completed `company_profile_crawl` stages (`source_stage` events): the accepted rows exist before the phase starts and the 90–365 day freshness policy cannot change a share inside the window, so only a crawl in the window shows the phase ran | 24 hours | not set | — |

## Preconditions

Do not start recruiting until every line holds for the release under test.

- **An environment with real inventory exists.** The repository defines no
  staging deployment (see "Observed state when this section was written" in
  `docs/rollout-gates.md`), so the environment in D3 has to be stood up, or
  the documented alternative chosen.
- **[#555](https://github.com/norcalipa/crank/issues/555) is fixed — open
  when this was written.** A hard precondition for any environment deployed
  by this repository's workflows, production included. Every deploy reverts
  out-of-band `kubectl` changes and blanks the capability credentials, and
  committing the reply flag without a surviving credential takes the web
  pods out of readiness ("Durable enablement rule" in
  `docs/rollout-gates.md`). Until it is fixed the canary alternative in D3
  cannot be prepared, and nobody should commit a capability flag to try.
- **The readiness record is taken on that environment.** Run
  `python manage.py readiness_baseline --out <file>` and keep
  `source_version`, `fixtures.revision` and `release_verdict`. The record
  must show the orchestrator provider, at least one approved and enabled
  source and no inventory violations.
- **Demo or fixture-only sessions do not count.** Canned replies
  (`JOB_SEARCH_PROVIDER=demo`) and synthetic listings (`seed_e2e`) are not
  source-backed data. A session run on them is a rehearsal and is not entered
  in the results record. Fixtures may supply dependable unknown and stale
  facts for T4 on a staging instance; the record then shows
  `fixtures.present: true` and its `release_verdict` is not production ready,
  which is correct.
- **Automated checks are green on the same SHA.** The Playwright Django
  workflow passes and `python manage.py rollback_drill --json` reports
  passed.
- **The surfaces under test are final.** That means every issue in the
  table below is closed and its change is in the release under test. Tasks
  are written as user goals because these surfaces were still changing when
  this was written. Re-read every task and probe against the release SHA
  before the first session, and correct any wording that no longer matches
  what a participant will see.

| Blocking issue | What it changes | Affects | State on 2026-10-08 |
|---|---|---|---|
| #551 | evidence status on job and assistant cards | T2, T4, C3, C4 | open (pull request) |
| #489 | job cards and posting links | T3 | open |
| #486 | company details in the workspace | T2 | open |
| #488 | help and conversation copy | A1, A2 | open |
| #536 | assistant listing tool can show hidden employers | wrong facts shown as confirmed | open |
| #537 | office-policy parser misreads "no office required" | wrong facts shown as confirmed | open |
| #548 | value readers misread RTO and vesting prose | wrong facts shown as confirmed | open |
| #555 | deploys blank credentials and revert flags | the environment itself | open |

Check each state again before recruiting; this table is a snapshot.

## Participants and recruiting criteria

- Five participants, recorded only as P1–P5.
- Both kinds are represented: at least one **fresh** participant (has never
  used the product) and at least one **returning** participant (has used it
  before, with saved priorities and at least one earlier conversation).
- None is a contributor to this project.
- Each is someone who would plausibly use the product: currently or recently
  looking for a job, or evaluating employers.
- At least one session is run at phone width.
- Returning participants use a throwaway account prepared for the session,
  not their own.

## Consent and recording checklist

Before each session, the moderator confirms aloud and ticks:

- [ ] The participant knows the session tests the product, not them, and can
      stop at any time without giving a reason.
- [ ] The participant knows what is written down: task outcomes, timings,
      probe answers and paraphrased confusion notes, under a participant
      number.
- [ ] The participant knows the results are **published**: the results
      record goes in a pull request to this repository, which is public, and
      git history keeps it, so a merged row cannot be removed. Per
      participant number it holds fresh or returning, screen width, task
      outcomes, timings and probe answers, beside the session dates. The
      participant can withdraw their row until the pull request is merged.
- [ ] The participant knows what the moderator does not write down: their
      name, contact details, employer, real pay, real job preferences, and
      the words they type or the assistant replies.
- [ ] The participant knows that **the product itself keeps what they
      type**: chat messages and saved priorities are stored under the
      throwaway account, and each message and the saved priorities are sent
      to the external language-model provider that writes the replies. The
      stored copies are kept until the account is deleted, which happens
      after the round (see "Deleting session data"); the copy sent to the
      provider cannot be deleted from here.
- [ ] The participant has been asked **not to type real personal details**
      — no real name, employer, pay, location or contact details — in
      priorities or in chat.
- [ ] The participant has agreed to notes being taken. Audio or screen
      recording happens only with separate explicit agreement, and a
      recording is never stored in the repository or attached to an issue.
      A recording lives only on the moderator's own device. A conferencing
      tool that records or transcribes a remote session keeps its own copy
      under its own terms, so it is switched off unless the participant has
      agreed to that copy, and the moderator deletes it at the retention
      limit (D6).
- [ ] The participant has been asked to use invented priorities rather than
      their real ones.
- [ ] The participant is using a throwaway account.

## Data-minimisation rules

The following are never recorded in the repository, in an issue or pull
request, or in the results record:

- names;
- e-mail addresses;
- employers;
- real compensation figures;
- real preference values;
- account identifiers;
- prompts and replies verbatim;
- screen recordings stored in the repository.

Further rules:

- Participants are recorded as P1–P5 and nothing else. The mapping from
  number to person is kept outside the repository by the moderator. It, the
  recruiting messages and the calendar invitations identify the participants
  more directly than any note, and are destroyed with the raw notes (D6).
- Confusion is paraphrased ("did not notice the date under the reason"),
  never quoted from what the participant typed or what the assistant
  replied.
- **Retention limit for raw notes:** raw notes, any recording or
  transcript, the participant-number mapping, the recruiting messages and
  the calendar invitations are destroyed when the results record is merged, and in any case no later than
  30 days after the last session (D6) — including when the round is
  abandoned or has to be repeated. They never enter the repository.

### What the product stores during a session

The rules above govern the moderator's records. The product keeps its own:

- Every chat message, the participant's and the assistant's, is stored
  verbatim (`JobSearchMessage.content`). **Every message of every
  conversation is kept until the conversation or the account is deleted** —
  including a conversation the participant closed by starting a new one.
  Nothing in the repository removes messages by age or by number.
- The product shows only the newest 50 messages of a conversation
  (`JOB_SEARCH_MESSAGES_RETENTION`). That setting limits what the page and
  the API return; it does not limit what is stored.
- Saved priorities are stored (`UserPreference`) until changed or reset, and
  are deleted with the account.
- With the orchestrator provider that D3 requires, the conversation text and
  the saved priorities are sent to the external language-model provider on
  every turn. How long the provider keeps them is set by the provider's
  terms, not by this repository.

### Deleting session data

After the last session, and before the results record is merged:

1. A superuser deletes each throwaway account in Django admin
   (**Authentication and Authorization → Users → Delete**). This is an
   existing path: the account's conversations, messages, saved priorities,
   matches and submitted corrections are deleted with it
   (`on_delete=CASCADE`; a test in
   `crank/tests/test_release_decision_gates.py` drives the admin delete page
   and checks each of these is gone). A
   participant can also delete a single conversation themselves with the
   product's own delete-conversation control
   (`agent_conversation_delete`), but that leaves the account and its saved
   priorities, so it does not replace this step.
2. **The repository owner confirms the deletion** and records the date in the
   results record, in one of two ways: the admin user list no longer shows
   any session account; or, on an environment with no other users, a
   readiness record taken after deletion shows `conversations`, `messages`
   and `saved_preferences` in `data_counts` back at the values of a record
   taken before the first session.
3. There is no command or page that deletes a set of accounts in one step,
   and none is added here. If the manual step proves error-prone, file a
   follow-up issue for a bounded cleanup command.
4. Nothing here can delete what was already sent to the model provider.
   Before recruiting, the owner checks the provider's retention terms and
   decides whether they are acceptable for the round.

## Moderator script

1. Read the consent checklist and tick it, including the two items about
   what the product stores and not typing real personal details.
2. Say: "I will read you a goal. Please do it the way you would on your own
   and say what you are thinking. I cannot help while you work; that is so we
   learn what the product needs to explain better."
3. Read each task exactly as written, start the timer, and stay silent.
4. Stop a task when the participant says they are done, gives up, or five
   minutes pass without progress. Record the outcome.
5. After the tasks, ask the four comprehension probes exactly as written.
6. Thank the participant. Do not explain correct answers until all tasks and
   probes are finished.

**Assistance** is any moderator action that gives the participant information
they did not find themselves: pointing at or naming a control, saying where
to look, explaining what a label or state means, confirming or denying that
an answer is right, rephrasing the task to hint at the path, or operating the
product for them. A task with any assistance is recorded as **assisted**,
however small the hint.

These are not assistance: re-reading the task verbatim; "please keep thinking
aloud"; fixing something unrelated to the product (device, network, sign-in
to the throwaway account).

## Task list

T1 to T3, in order, are the **core task**. Success conditions are observable
and stated in terms of the participant's goal.

| Id | Read to the participant | Success is observed when |
|---|---|---|
| T1 | "Tell the product what matters to you in a job, using the invented priorities we agreed." | The participant's priorities are saved and they say, unprompted or when asked "is that done?", that the product now has them. |
| T2 | "Pick one result. Tell me why the product thinks it suits you, and when the information behind that reason was last checked." | The participant states a reason that the product shows for that result and a date or age that the product shows for its source. |
| T3 | "Find a job at that company that is open now and get to the place where you could apply." | The original posting of a job the product shows as available is open in front of the participant. |
| T4 | "Find one thing about this company or job that the product does not know, or that may be out of date." | The participant points to a fact the product marks as unknown or stale and says which of the two it is. |

## Comprehension probes

Asked after the tasks, with the result the participant used still on screen.

| Id | Question | Counts as correct |
|---|---|---|
| C1 — company score | "What does this company's score tell you? Would it be different for someone else?" | Says it describes the company in general and is the same for everyone; does not say it reflects their own priorities. |
| C2 — personal fit | "What does the product mean when it says this suits you? What would change it?" | Says it comes from the priorities they set and would change if those changed; does not treat it as a rating of the company. |
| C3 — data coverage | "How much does the product know about this company? How can you tell?" | Says some things are known and others are not, points to where that is shown, and does not read a missing fact as a bad fact. |
| C4 — uncertainty | "Is there anything here you would check yourself before relying on it? Why?" | Names a fact marked unknown or stale and says it is unverified or possibly out of date, rather than true or false. |

## Measures

Per participant, per task:

- **Outcome:** unassisted, assisted or failed.
- **Time to useful result:** seconds from the end of the task read-out to the
  success condition (for T3, to the posting being open).
- **Confusion notes:** paraphrased, each given a severity.

Per participant, per probe: correct or not correct against the rubric.

Telemetry can corroborate only part of this. `preference_decision` covers T1
and `assistant_first_result` covers time to a first result
(`docs/monitoring.md`, "Assistant and data-freshness metrics"). Looking at a
reason, reading a source date and opening a posting happen in the browser and
emit no event, so the moderator's observation is the only measure of T2, T3
and T4.

## Severity rubric

| Severity | Meaning | Required action |
|---|---|---|
| S1 | A participant could not complete a core task, or completed it believing something false. | Follow-up issue; fixed before the release decision. |
| S2 | A participant completed the task only with assistance, or drew a wrong conclusion in a probe. | Follow-up issue; fixed or explicitly accepted before the release decision. |
| S3 | A participant was slowed or visibly unsure but recovered alone. | Recorded; follow-up issue at the owner's discretion. |
| S4 | Cosmetic or preference remark with no effect on the outcome. | Recorded only. |

## Findings template

One row per finding. No names, no quotations.

| Id | Participant(s) | Task or probe | What happened (paraphrased) | Severity | Follow-up issue |
|---|---|---|---|---|---|
| F1 | _P?_ | _T? / C?_ | _…_ | _S?_ | _#…_ |

## Pass criteria

- **A1 — core task:** at least 4 of 5 participants complete T1–T3 unassisted.
  Every failed or assisted core task produces a specific follow-up issue.
- **A2 — four distinctions:** the bar in D4 — at least 4 of 5 participants
  answer all four probes (C1–C4) correctly.
- T4 is reported alongside A1 as the measure of identifying an unknown or
  stale fact; a failure is an S1 or S2 finding.
- Every S1 and S2 finding has a follow-up issue that is fixed or explicitly
  accepted before a release decision is recorded.

## Failure and retry matrix

This is the matrix the acceptance statement "no lost drafts or duplicate
turns in the failure/retry matrix" refers to. Each row is a failure mode with
its recovery action. Evidence is cited as `` `path::test name` ``; a test in
`crank/tests/test_release_decision_gates.py` fails if a cited file or test
name does not exist. A citation shows that the test exists, not that it ran:
cells marked **browser evidence pending 492c** cite a browser test that is
currently skipped and are not passing evidence.

| Failure mode | Recovery action | Draft preserved | No duplicate turn | Evidence |
|---|---|---|---|---|
| Provider error (503 `assistant_unavailable`) | Retry | yes — the sent turn stays visible | yes | `static/js/JobSearchChat.test.tsx::surfaces assistant_unavailable error with data-error-type and retries`; `crank/tests/views/test_job_search.py::test_service_error_is_stable_and_retry_does_not_duplicate`. Browser evidence pending 492c: `e2e/django/regression.spec.ts::failed turn keeps the persisted user message visible, with Retry, across reload` |
| Provider timeout | Retry | yes — the sent turn stays visible | yes | `static/js/JobSearchChat.test.tsx::surfaces provider_timeout error with data-error-type`; `crank/tests/views/test_job_search.py::test_provider_timeout_returns_504`; `crank/tests/views/test_job_search.py::test_typed_error_preserves_user_turn_for_retry` |
| Network failure before the server received the turn | Send again from the restored draft | yes — restored to the composer | yes — nothing was stored | `static/js/JobSearchChat.test.tsx::a network failure the server never received is restored as a draft`; `static/js/JobSearchChat.test.tsx::a network failure the server did receive adopts the server state` |
| Stop while a turn is in flight | Send again, or check whether it arrived | yes | yes | `static/js/JobSearchChat.test.tsx::stop before the server received the turn restores it as a draft`; `static/js/JobSearchChat.test.tsx::stop with a pending server turn shows the honest not-arrived panel with a check action` |
| Double-click on Retry | None needed | yes | yes — one outcome | `static/js/JobSearchChat.test.tsx::double-clicking retry applies at most one outcome (client pending guard)`; `crank/tests/views/test_job_search.py::test_idempotent_retry_does_not_duplicate_messages`; operator-run on MySQL: `crank/tests/test_mysql_concurrency.py::test_serialized_replay_keeps_message_count_stable` |
| Retry limit reached | Edit as new message | yes — the text is offered for editing | yes | `static/js/JobSearchChat.test.tsx::a server-loaded exhausted turn disables Retry and points to Edit`; `crank/tests/views/test_job_search.py::test_retry_limit_reached_after_attempt_cap`. Browser evidence pending 492c: `e2e/django/ac7-surfaces.spec.ts::cancel abandons the failed turn with a visible outcome and no duplicate reply` |
| Reload during a failed turn | Retry after reload | yes — the failed turn is reloaded from the server | yes — same key | `static/js/JobSearchChat.test.tsx::retrying a server-loaded failed turn resends the same key without duplicating history`; `static/js/JobSearchChat.test.tsx::a marker after a failed (typed) send still reconciles against the server on reload`. Browser evidence pending 492c: `e2e/django/regression.spec.ts::failed turn keeps the persisted user message visible, with Retry, across reload` |
| Sign-in handoff or session expiry | Sign in, then send | yes — adopted into the composer after sign-in | yes — nothing is sent while signed out | `static/js/JobSearchChat.test.tsx::a pending draft is adopted into the composer once a conversation exists after sign-in`; `static/js/JobSearchChat.test.tsx::visitorState=session_expired renders whatever signedOutMessage the server resolved` |
| Second tab | Continue in either tab | yes — both unsent drafts are kept | yes — one stored turn per key | `static/js/JobSearchChat.test.tsx::two tabs confirmed absent keep both unsent markers; the newest surfaces`; `crank/tests/test_schema_compat.py::test_unique_constraint_blocks_duplicate_idempotency_key` |

## What only the owner can do

None of these can be done by a pull request from an implementer.

1. Choose and prepare the session environment (D3). For any environment
   deployed by this repository's workflows this waits for #555; do not
   commit a capability flag before it is fixed.
2. Confirm every blocking issue in the Preconditions table is closed and in
   the release under test.
3. Check the model provider's retention terms for conversation text and
   saved priorities, and decide whether they are acceptable for the round.
4. Record the release under test with `readiness_baseline`. Keep the
   `data_counts` of this record: it is the "before" for step 9.
5. Confirm the automated preconditions on that SHA.
6. Recruit five participants to the criteria above.
7. Run the five sessions with the moderator script.
8. Score the round against A1 and A2.
9. Delete the throwaway accounts and confirm the deletion
   ("Deleting session data"). Destroy the raw notes, recordings, mapping,
   recruiting messages and invitations by the limit in D6. Keep one seeded account that was not used by a participant and
   holds stored results until step 11 is done, so that step has something
   to show preserved; delete it afterwards.
10. File a follow-up issue for every failed or assisted core task and every
    S1 or S2 finding; fix or explicitly accept each before the release
    decision.
11. Exercise disablement and rollback in the chosen environment: take a
    readiness record, switch off the provider
    (`CapabilitySwitch(interactive_agent)`) and one source
    (`JobSourceCatalog.enabled`), confirm direct priority editing and stored
    results still work, re-enable, take a second record, and compare
    `data_counts`. This restarts the window of every gate in the phases it
    touches (`docs/monitoring.md`, "A clean window").
12. Complete the manual accessibility evidence still pending in
    `docs/e2e-validation.md` ("Manual evidence pending").
13. Once #555 is fixed, enable each phase durably, one pull request per
    capability and one for the master flag, in the order of the phase table
    in `docs/rollout-gates.md`, running the post-merge check each time.
14. Lock a sample floor for every gate and confirm the alert policies exist
    in the alerting tool ("Sample floors and alert policies to lock").
15. Observe each enabled phase for a clean window, run the gate queries, and
    decide expand, hold or roll back by the procedure in
    `docs/monitoring.md`. Baseline-required gates need at least 14 days of
    data with the capability enabled.
16. Lock thresholds after the baseline in a small follow-up pull request.
17. Record each decision in the decision record template, signed as D5
    defines and within what D7 allows.
18. Complete #492 with the filled results record.

## Results record

Empty until the round is run. Filled in by the owner (D5).

### Release under test

| Field | Value |
|---|---|
| Release SHA (`source_version`) | _…_ |
| Environment (`env`) | _…_ |
| Fixture revision (`fixtures.revision`) | _…_ |
| `release_verdict` | _…_ |
| Session dates | _…_ |
| Session accounts deleted (date, confirmed by) | _…_ |
| Raw notes, recordings, participant mapping, recruiting messages and invitations destroyed (date) | _…_ |

### Outcomes

Outcome is U (unassisted), A (assisted) or F (failed); time is seconds to
useful result; probes are ✓ or ✗.

| Participant | Fresh / returning | Width | T1 | T2 | T3 | T4 | C1 | C2 | C3 | C4 |
|---|---|---|---|---|---|---|---|---|---|---|
| P1 | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ |
| P2 | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ |
| P3 | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ |
| P4 | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ |
| P5 | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ | _…_ |

### Score

| Criterion | Result | Pass? |
|---|---|---|
| A1 — participants completing T1–T3 unassisted | _… of 5_ | _…_ |
| A2 — participants answering C1–C4 correctly | _… of 5_ | _…_ |
| T4 — participants identifying an unknown or stale fact | _… of 5_ | _reported_ |

### Findings and follow-up

Use the [findings template](#findings-template).

### Sign-off

| Role signed for | GitHub handle | Date |
|---|---|---|
| _…_ | _…_ | _…_ |
