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
| D1 | Shell rollback | **default — owner may change:** redeploying the previous image is accepted as the shell's rollback; `assistant_shell` stays `planned` and no shell switch is built. |
| D2 | Gate thresholds | **default — owner may change:** existing #328 stage numbers are reused as `provisional`; every other new gate is `baseline_required` until 14 days of data exist with the capability enabled; no new alerts. Values live in `release_gates:` in `docs/monitoring.yaml`. |
| D3 | Where sessions run | **default — owner may change:** a staging instance (`ENV=staging`, orchestrator provider, at least one real approved source). Documented alternative: production as an internal canary with throwaway accounts, after durable enablement. |
| D4 | Pass bar for A2 | **default — owner may change:** at least 4 of 5 participants answer all four comprehension probes (C1–C4) correctly. |
| D5 | Sign-offs and where evidence lives | **default — owner may change:** the repository owner signs each role by GitHub handle and date; results are recorded by an owner-authored docs pull request that fills the results record; raw notes never enter the repository. |

## Preconditions

Do not start recruiting until every line holds for the release under test.

- **An environment with real inventory exists.** The repository defines no
  staging deployment (see "Observed state when this section was written" in
  `docs/rollout-gates.md`), so the environment in D3 has to be stood up, or
  the documented alternative chosen. Enablement must follow the durable
  enablement rule in that section, because out-of-band `kubectl` changes are
  reverted by the next deploy.
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
- **The surfaces under test are final.** Tasks below are written as user
  goals because the job cards, company view, evidence labels and help copy
  were still changing when this was written. Re-read every task and probe
  against the release SHA before the first session, and correct any wording
  that no longer matches what a participant will see.

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
- [ ] The participant knows what is not kept: their name, contact details,
      employer, real pay, real job preferences and anything they typed.
- [ ] The participant has agreed to notes being taken. Audio or screen
      recording happens only with separate explicit agreement, and a
      recording is never stored in the repository or attached to an issue.
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
  number to person is kept outside the repository by the moderator.
- Confusion is paraphrased ("did not notice the date under the reason"),
  never quoted from what the participant typed or what the assistant
  replied.
- **Retention limit for raw notes:** raw notes and any recording are
  destroyed when the results record is merged. They are not kept past that
  point and never enter the repository.

## Moderator script

1. Read the consent checklist and tick it.
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

1. Choose and prepare the session environment (D3), following the durable
   enablement rule and #453.
2. Record the release under test with `readiness_baseline`.
3. Confirm the automated preconditions on that SHA.
4. Recruit five participants to the criteria above.
5. Run the five sessions with the moderator script.
6. Score the round against A1 and A2.
7. File a follow-up issue for every failed or assisted core task and every S1
   or S2 finding; fix or explicitly accept each before the release decision.
8. Exercise disablement and rollback in the chosen environment: take a
   readiness record, switch off the provider
   (`CapabilitySwitch(interactive_agent)`) and one source
   (`JobSourceCatalog.enabled`), confirm direct priority editing and stored
   results still work, re-enable, take a second record, and compare
   `data_counts`.
9. Complete the manual accessibility evidence still pending in
   `docs/e2e-validation.md` ("Manual evidence pending").
10. Enable each phase durably, one pull request per capability, in the order
    of the phase table in `docs/rollout-gates.md`.
11. Observe each enabled phase for its window, run the gate queries, and
    decide expand, hold or roll back. Baseline-required gates need at least
    14 days of data with the capability enabled.
12. Lock thresholds after the baseline in a small follow-up pull request.
13. Record each decision in the decision record template with a named
    sign-off (D5).
14. Close #492 with the filled results record.

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
