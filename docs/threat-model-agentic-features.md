<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Threat model: agentic job-search features

Review date: 2026-09-14 (updated by #487; previous review 2026-08-12)
Scope: authenticated chat, preferences, score/source ingestion, listing
resolution, deterministic matching, owner-scoped match presentation, and the
assistant-surface boundaries added by epic UX (typed page context, allowlisted
UI actions, proposed preference patches, cached account state, late replies,
and evidence status).

This review is intentionally bounded to the existing agentic feature paths. It
covers the offline security/integration suite in
`crank/tests/security/test_phase4_security.py`, the focused boundary tests
listed below, and the #487 extensions. No live provider or source traffic is
required. **A fixture-based suite is not a completed audit**; residual risks
are stated per boundary below and owners are named.

## Assets and actors

### Assets

- User identity and tenant ownership relationships.
- Preference JSON, generated preference markdown, and conversation/message text.
- Persisted scores, source provenance, job listings, employer-resolution data,
  and user-specific matches.
- Provider/source credentials, request metadata, correlation identifiers, and
  operational logs/events.
- Server policy: tool allowlists, prompt instructions, source approval, URL and
  DNS restrictions, retention limits, and matching rules.

### Actors

- Authenticated user, including a malicious user attempting identifier
  guessing or crafted messages.
- Unauthenticated internet client.
- Compromised or malicious model/provider output.
- Malicious or compromised external source response, including hostile text,
  URLs, redirects, and DNS answers.
- Other application tenant and staff/operator. Staff access is separate from
  user APIs and is audited by the existing admin controls.

## Trust boundaries and controls

| Boundary | Abuse cases | Mitigations | Tests/evidence | Residual risk / owner |
| --- | --- | --- | --- | --- |
| HTTP client -> authenticated chat | Anonymous access, CSRF, oversized/malformed body, cross-user conversation ID guessing, replay duplication | Django authentication/CSRF, bounded JSON/message sizes, owner-scoped queries, UUID idempotency keys, stable non-sensitive errors | `crank/tests/views/test_job_search.py`; phase-4 owner/markup/retention tests | Concurrent request races need production DB/load testing; owner: web/API maintainers |
| User/model text -> prompt context | Prompt injection, stored/reflected markup, arbitrary URL/tool/policy requests, oversized history/preferences | Server-built versioned system prompt, untrusted-data labels, bounded deterministic context, text-only UI, no model-controlled URL/tool surface | `crank/tests/agents/test_context.py`, `test_service.py`, `test_types.py`, `static/js/JobSearchChat.test.tsx`, phase-4 injection tests | Provider-side policy failures remain possible; owner: agent maintainers |
| Provider output -> preference/presentation state | Malformed JSON, extra keys, guessed IDs, unchecked patch, oversized output, secret-bearing exception | Strict output schema, unknown-key rejection, bounded message/patch depth/keys/bytes, server catalog citation check, preference service validation before apply | `crank/tests/agents/test_types.py`, `test_service.py`, phase-4 malformed-output tests | A valid but undesirable user-requested patch still depends on schema policy; owner: preference/agent maintainers |
| Source catalog -> network transport | HTTP downgrade, credential-bearing URL, unapproved host/port, redirect to attacker/private host, DNS rebinding/private/link-local/loopback, oversized body | Code-owned host allowlist, HTTPS-only, no URL credentials/nonstandard ports, manual bounded redirects, resolve every hop, reject all blocked/unresolved answers, byte/content-type caps, bounded retries | `crank/tests/agents/sources/test_transport.py`, `test_yelp.py`, `test_usajobs_adapter.py`, phase-4 URL tests | DNS resolution can change after the check at the network layer; owner: source/platform maintainers |
| Source payload -> application records | Markup/instruction injection, schema drift, secret/raw-body retention, unchecked employer creation | Typed adapters, strip/bound listing text, metadata rejection, no automatic organization creation, deterministic alias resolution, unresolved review queue | `crank/tests/agents/test_job_ingest.py`, `test_employer_resolution.py`, `test_job_models.py`, phase-4 ingestion tests | Source terms/licensing remain source-specific blockers; owner: source owners |
| Normalized score -> persistence | Malformed values, identity guessing/ambiguity, duplicate/replayed writes, secret provenance | Decimal reject-not-clamp normalization, curated identity mappings, transactional active-row replacement, idempotent provenance identity, provenance allowlist/redaction | `crank/tests/agents/sources/test_normalize.py`, `crank/tests/services/test_score_persistence.py`, phase-4 score tests | Database concurrency needs backend-specific verification; owner: score maintainers |
| Listing/match records -> presentation | Cross-user match access, guessed IDs, inactive/dismissed listing exposure, stored markup | `user=request.user` filters, active-listing filters, dismissed filtering, bounded JSON projection, text-only rendering | `crank/tests/views/test_job_matches.py`, `test_match_persist.py`, phase-4 owner tests | Staff/admin access is intentionally separate; owner: matching/API maintainers |
| User records -> export/reset/delete/retention | Exporting another tenant, stale data after deletion, excessive history, preference audit leakage | Owner-scoped preference/conversation operations, cascade delete, contents-free audits, configured message export cap, reset archives chat, inactive/dismissed matches excluded | `crank/tests/services/test_preferences.py`, `crank/tests/views/test_job_search.py`, `test_job_matches.py`, phase-4 retention tests | Preference/match retention is policy-driven rather than a background purge in this phase; owner: privacy/product maintainers |
| Runtime -> logs/events | Prompt/message/source secrets or sensitive content in logs/New Relic | Correlation/status/counters only, sanitized bounded error summaries, no prompt/message logging, auth never in transport errors | `crank/tests/test_agent_runs.py`, `test_llm.py`, transport redaction tests, phase-4 negative log/event assertions | Downstream infrastructure must preserve field filtering; owner: platform/observability maintainers |

## Assistant-surface boundaries (epic UX, #487)

Each control below is marked **implemented** (shipped with tests in this
change) or **pending** (owned by its sibling ticket; asserted nowhere until it
lands — implemented-before-documented rule, per #463's registry).

### Typed page context

- **Status: pending** — owner: #477/#478 (typed context and action vocabulary),
  surface #471 (sidebar shell); preference/background consumers #465/#484/#466/#460.
- **Asset:** the bounded, typed page-context objects the assistant may read
  (server-built, schema-validated); never raw DOM or client-controlled JSON.
- **Planned controls:** server-owned construction and bounds (depth/size caps
  via the `types.py` bounded-scalar/patch primitives), untrusted-data labels,
  no model-controlled field names. Until the owning tickets land, the chat
  context remains the existing deterministic builder
  (`crank/agents/job_search/context.py`) whose bounds are tested in
  `crank/tests/agents/test_context.py`.
- **Evidence:** existing `test_context.py` / `test_types.py`; no new controls
  claimed here.

### Allowlisted UI actions

- **Status: pending** — owner: #478 (action vocabulary), surface #471.
- **Asset:** the fixed, code-owned action/tool allowlist. Today this is the
  `tools.py` module surface (plain server-wired functions, no registry the
  model can expand) and the fixed `AssistantCompletion` schema; the future UI
  action vocabulary must follow the same pattern.
- **Implemented controls pinned by #487:** hostile model output cannot add
  actions/tools/policy keys (`AssistantCompletion.from_json` rejects unknown
  keys and oversized values); malformed action payloads (unknown names, wrong
  types, oversized values) and patch keys outside the validated spec fail
  closed (`MalformedActionPayloadTests` in
  `crank/tests/security/test_phase4_security.py`); prompt-injection fixtures
  through the provider are carried as inert display text and never expand the
  tool surface (`test_prompt_injection_fixture_cannot_invoke_tools`);
  `tools.py` is asserted to stay a fixed, code-owned allowlist.
- **Pending:** the UI action vocabulary itself and its dispatch boundary are
  built and validated by #478/#471.

### Proposed preference patches

- **Status: implemented (this change).**
- **Asset:** the user's stored `UserPreference` row (version-1 schema),
  projected markdown, and the audit trail.
- **Control chain:** model output → `AssistantCompletion.from_json` bounds →
  `validate_patch` (typed schema, unknown fields fail) →
  `apply_patch_to_user(user, patch, expected_modified)` under
  `select_for_update` with the optimistic `modified` check (`_check_stale`) →
  owner-scoped persistence → contents-free audit rows.
- **Wiring (#487):** the transport provider captures the preference row's
  `modified` **at turn start, in the same single read as the preference
  markdown snapshot** (`OrchestratorJobSearchProvider._read_preference_snapshot`),
  and passes it through the turn as `expected_modified`; the prompt snapshot
  and the concurrency baseline can therefore never drift apart. A row
  changed mid-turn (user edit or reset while a slow reply was in flight)
  raises `StalePreferenceError`, mapped to the typed `PreferenceStaleError`
  and then to a stable **409 `preference_stale`** envelope. The stale patch
  is never applied; the persisted user turn remains retryable with the same
  idempotency key.
- **Baseline semantics:** a turn that starts with no preference row carries
  the `PREFERENCE_ABSENT` sentinel instead of a timestamp — the patch applies
  only if the row is *still* absent at commit time. A row created mid-turn
  makes the patch stale (no overwrite), and a row *deleted* mid-turn makes
  the patch stale too (the patch never re-creates/resurrects deleted
  preference state).
- **Fail-closed baseline (review round 2, MAJOR-4):** when the baseline
  cannot be captured at turn start — or a writer port cannot carry it — the
  patch path **aborts** with a typed `PreferenceVersionUnavailableError` mapped
  to the same stable, retryable 409 `preference_stale` envelope (monitoring
  reason code `preference_version_unavailable`). The stale check is never
  silently skipped for a writer port. **The only legacy allowance** is a port
  that demonstrably has no writer: ports declaring `writable = False` — today
  only the no-owner `_NullPreferenceService`, whose `apply_patch` is a
  documented no-op returning `False` — may proceed without a baseline. Any
  other port without the capability defaults to being treated as a writer
  and fails closed.
- **Lifecycle ordering (review round 2, MAJOR-2; single-commit window closed
  in the fix-verification round):** a proposed patch is committed only after
  a per-turn **lifecycle guard** verifies the conversation is still active
  **inside the same database transaction as the preference write**. The guard
  uses the repo's write-first conditional claim (`UPDATE ... WHERE pk AND
  owner AND active=True`) — deliberately not `select_for_update`, which is a
  no-op on SQLite — so the claim takes the conversation row's write lock on
  every backend. That claim, the patch write, and the reply persistence
  share ONE commit boundary (see "Late replies" below): a mid-turn
  reset/delete therefore either aborts the patch before the single commit
  (`ConversationClosedError` → 409 `conversation_closed`, nothing persisted)
  or blocks on the row lock until the whole turn commits. Backend lock
  contention (SQLite single-writer fail-fast, MySQL lock-wait/deadlock) maps
  to the same retryable 409 envelopes — `conversation_closed` for lifecycle
  writes, `preference_stale` for the patch write — never a 500.
- **Tests:** `crank/tests/views/test_job_search.py`
  (`StalePreferenceAndLateReplyTests` — stale 409 + no overwrite + retry,
  matching-version applies, adapter mapping, reset/delete-with-patch changing
  neither persistence domain, baseline-capture failure failing closed),
  `crank/tests/agents/test_service.py` (`TestPreferenceBaselineGuards` —
  turn-start capture, fail-closed writer ports, legacy no-writer allowance),
  `crank/tests/security/test_phase4_security.py`
  (`MalformedActionPayloadTests`).
- **Residual risk:** a valid-but-undesirable user-requested patch still
  depends on schema policy, not on this check; owner: preference/agent
  maintainers.

### Cached account state

- **Status: implemented (tests pinned by #487; caching semantics owned by #470).**
- **Asset:** public cache entries (`cache_page`-decorated views —
  `algo/<id>/` IndexView, funding/RTO choices — and the organization API
  caches) and private session state.
- **Controls:** public endpoints are deliberately unauthenticated; the
  requirement is separation, not access control. Public payloads must be
  identical for different authenticated accounts and contain no username,
  preference, or conversation content; rate-limit keys (`job_search_rl:*`)
  and session keys never contain message content — counters only.
- **Review round 2 finding (MINOR-1), fixed:** exercising the previously
  untested `algo/<id>/` IndexView against two accounts proved the shared
  `cache_page` entry served the first authenticated requester's **username**
  to every later visitor (the shared navigation chrome renders
  `{{ user.username }}`). The page cache for that view is now
  **anonymous-only**, using the established `cache_page_if_anonymous_method`
  pattern: anonymous traffic keeps the page cache, and authenticated
  requests render fresh — they neither read another account's cached chrome
  nor poison the shared entry with their own. The funding/RTO choice JSON
  and organization API caches carry no account data (pinned: account B is
  served the warmed entry even after the backing data changes, and the
  payload stays bounded to public fields).
- **Tests:** `PublicCacheSeparationTests` —
  `test_algo_index_view_page_cache_is_anonymous_only_and_public`,
  `test_funding_choices_second_account_served_from_cache`,
  `test_rto_choices_second_account_served_from_cache`,
  `test_organization_api_second_account_served_from_cache`, and
  `test_no_message_content_in_any_cache_key_or_value` (keys AND values) in
  `crank/tests/security/test_phase4_security.py`.
- **Residual risk:** page-cache poisoning via shared-cache key collisions is
  an infrastructure concern (`CACHE_MIDDLEWARE_KEY_PREFIX` deployment
  config), not covered by fixtures; owner: platform maintainers (#470).

### Late replies

- **Status: implemented (this change; serialization boundary hardened in
  review round 2, and the guard→patch→reply window closed into a single
  commit boundary in the fix-verification round, with the retry contract
  made real).**
- **Abuse case:** a slow/cancelled provider reply completing after the
  conversation was reset or deleted mid-turn — previously it could attach an
  assistant message to a closed conversation.
- **Control:** the whole persistence tail of a turn — the lifecycle guard's
  write-first conversation-row claim (`UPDATE ... WHERE pk AND owner AND
  active=True`), the optional preference-patch write, and the assistant-
  message insert — runs in **ONE database transaction** with a single commit
  boundary (`OrchestratorJobSearchProvider` → orchestrator `_commit_turn_writes`
  invoking the view's reply-persistence hook; issue #487 review round 2,
  MAJOR-1). The claim is write-first, not `select_for_update`, so it takes
  the conversation row's write lock on every backend including SQLite. The
  reset, delete, and fresh-conversation endpoints use the **same write-first
  claim** inside their own transactions, giving every lifecycle writer and
  the guarded turn one shared serialization boundary: a reset/delete either
  commits before the claim — the claim matches no active row and the whole
  turn aborts with nothing persisted (stable **409 `conversation_closed`**)
  — or it blocks on the row lock until the turn's single commit lands the
  patch and the reply together, and only then closes the conversation; it
  can never interleave between the patch write and the reply insert, so a
  committed patch can never be stranded on a closed conversation. Inside the
  transaction a re-verify fails closed if lifecycle state flips in-window,
  and a vanished row surfaces as an FK `IntegrityError` — both discard the
  whole turn (patch included) and map to the 409. On SQLite the single-
  writer lock makes a contending lifecycle write fail fast with
  `database is locked` (deadlock avoidance) rather than block; the lifecycle
  endpoints therefore retry that transient contention in place (bounded),
  and any residual contention maps to the retryable 409 envelopes — never a
  500 (MAJOR-2 two-connection probe pinned).
- **Retryability (issue #487):** a `conversation_closed` turn stays retryable
  with the same idempotency key: the transport's Retry recovers by switching
  to the user's active conversation — the reset's fresh one, or a newly
  created one after delete — and replaying the retained user turn (same
  content, same key) there. The original user row is retained on the closed
  conversation; end-to-end retry-after-reset and retry-after-delete tests
  prove the retained turn is recoverable and completes with a 201.

```mermaid
sequenceDiagram
    participant C as Client
    participant V as agent_conversation_detail
    participant O as Orchestrator
    participant P as Provider gateway
    participant DB as Preference/Conversation store

    C->>V: POST message (idempotency_key)
    V->>DB: persist user turn (get_or_create)
    V->>DB: read preference snapshot + modified (ONE read, turn start)
    V->>O: run_turn(expected_modified, lifecycle_guard, persist_reply)
    O->>P: complete(...)
    Note over P: slow reply in flight;<br/>concurrent user edit bumps preference version<br/>or resets/deletes the conversation
    P-->>O: reply with proposed patch
    O->>DB: ONE transaction: write-first row claim + apply_patch(expected_modified) + insert reply
    alt preference changed mid-turn
        DB-->>O: StalePreferenceError (not applied)
        O-->>V: PreferenceStaleError
        V-->>C: 409 preference_stale (user turn retryable)
    else conversation reset/deleted before the claim (or in-window flip/vanish)
        O-->>V: ConversationClosedError (patch + reply discarded, single rollback)
        V-->>C: 409 conversation_closed (nothing persisted)
    else lock contention with a concurrent writer
        O-->>V: ConversationClosedError / PreferenceStaleError (retryable)
        V-->>C: 409 (user turn retryable; lifecycle endpoint retries in place)
    else version matches, conversation active
        O->>DB: COMMIT (patch + reply together, single boundary)
        V-->>C: 201 reply; a reset blocked on the row lock lands after the commit
    end
```

- **Tests:** `StalePreferenceAndLateReplyTests` (reset and delete mid-turn
  with a provider that mutates lifecycle state; preferences unchanged;
  reset/delete landing between the locked re-check and the insert — the
  insert is discarded and rolls back), `GuardedTurnCommitTests` (hook-path
  patch+reply rolling back together, database-locked contention mapping to
  the retryable 409 envelopes, end-to-end retry-after-reset and
  retry-after-delete recovery of the retained turn with the same
  idempotency key), and `GuardedTurnCommitConcurrencyTests` (two-connection
  probes: reset landing between the guard claim and the patch write, and
  after the patch write, both landing after the single commit with the turn
  returning 201 — the reviewer's post-patch/pre-reply repro; plus a reset
  fully completing before the claim yielding the 409 with nothing
  persisted), plus the real orchestrator/store integration tests covering
  reset/delete-with-proposed patch (both persistence domains unchanged).
- **Residual risk:** the hard row-lock boundary exists on locking backends
  (MySQL/PostgreSQL); the opt-in MySQL variants below assert it under real
  concurrency, and SQLite CI runs assert the in-transaction re-verify
  semantics. Lock behavior under production load is operational evidence;
  owner: web/API maintainers.

### Evidence status

- **Status: implemented (pinned by #487; status model owned by #460/#484 surfaces).**
- **Asset:** `CompanyProfileObservation.Status` — accepted/pending evidence
  states shown to users.
- **Controls:** evidence states are **server-derived only**;
  `mark_reviewed` validates the transition server-side and model output has
  no evidence field at all (`AssistantCompletion.from_json` rejects any
  `evidence` key; `test_evidence_status_is_server_derived_and_never_model_upgraded`).
- **Pending:** the user-facing evidence surface itself lands with #460/#484;
  its rendering boundaries must be validated when it exists.

### MySQL concurrency/deletion variants (opt-in)

The default suite runs on SQLite, where `select_for_update` is a no-op: the
optimistic `modified` check and the in-transaction re-verify are exercised,
the row lock is not. Backend-specific variants live in
`crank/tests/security/test_mysql_concurrency_variants.py` and are
**skipped unless `CRANK_MYSQL_TEST=1`** with the MySQL settings target
shipped as `crank.settings_mysql` (it inherits the default settings and
points `DATABASES` at an operator-provisioned MySQL server via the
`CRANK_MYSQL_*` environment variables — credentials stay in the environment,
never in the repository):

```bash
export CRANK_MYSQL_TEST=1
export DJANGO_SETTINGS_MODULE=crank.settings_mysql
export ENV=dev SECRET_KEY=... REDIS_MASTER_URL=redis://localhost:6379/0
export CRANK_MYSQL_NAME=crank_test CRANK_MYSQL_USER=... CRANK_MYSQL_PASSWORD=...
# Two concurrent patch writers starting from the SAME current version behind
# a barrier: exactly one applies, the loser receives StalePreferenceError,
# and no write is silently overwritten.
python -m pytest crank/tests/security/test_mysql_concurrency_variants.py::MySqlConcurrencyVariants::test_two_same_version_writers_exactly_one_applies -v
# Conversation deleted mid-turn: no assistant message row survives.
python -m pytest crank/tests/security/test_mysql_concurrency_variants.py::MySqlConcurrencyVariants::test_delete_during_turn -v
```

- **Residual risk:** these variants require operator-provisioned MySQL and
  are not run in CI; lock behavior under real load is asserted only by these
  opt-in runs; owner: platform maintainers.

## Retention and deletion policy covered by this phase

- Interactive chat exports only the newest `JOB_SEARCH_MESSAGES_RETENTION`
  messages in chronological order. A reset closes the old conversation and
  creates an empty active conversation; delete removes the conversation and its
  messages. User deletion cascades to chat records and preferences.
- Preference export returns the canonical document and generated markdown for
  the requesting user only. Reset restores schema-valid defaults. Delete removes
  the preference row; audit rows contain action metadata, not preference values.
- Matches are owner-scoped, and only active, non-dismissed listings are
  presented. A closed/expired listing is excluded from list/detail/actions.
  Match rows cascade with user deletion and listing deletion; dismissed rows
  remain stored as an application-state record but are not presented.
- Source raw response bodies, provider reasoning, hidden prompts, credentials,
  and complete request payloads are not retained by these paths.

## Review conclusion

The tested controls fail closed at each boundary: unauthorized objects resolve
as 404/not-found, invalid model/source data is rejected before writes, network
requests cannot follow unapproved/private destinations, stale preference
patches and late replies cannot overwrite or attach to newer/other state, and
sensitive values do not appear in logs/events/caches. Residual risks above
(concurrent-request races on production backends, cache-key infrastructure
config, source terms/licensing, schema-policy choices, and the surfaces still
pending their owning tickets — typed page context, UI action vocabulary,
preference/evidence/background UX) are stated per boundary with named owners.
These are operational or require separate production/load/legal review; they
are not widened by this phase. Passing the offline suites is **not** a
completed security audit.
