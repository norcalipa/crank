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
- **Wiring (#487):** the orchestrator captures the preference row's `modified`
  at turn start (`PreferenceService.current_modified()`) and passes it as
  `expected_modified` through `PreferenceService.apply_patch`; a row changed
  mid-turn (user edit or reset while a slow reply was in flight) raises
  `StalePreferenceError`, mapped to the typed `PreferenceStaleError` and then
  to a stable **409 `preference_stale`** envelope. The stale patch is never
  applied; the persisted user turn remains retryable with the same
  idempotency key.
- **Tests:** `crank/tests/views/test_job_search.py`
  (`StalePreferenceAndLateReplyTests` — stale 409 + no overwrite + retry,
  matching-version applies, adapter mapping),
  `crank/tests/security/test_phase4_security.py` (`MalformedActionPayloadTests`).
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
- **Tests:** `PublicCacheSeparationTests` and
  `test_no_message_content_in_any_cache_key_or_value` in
  `crank/tests/security/test_phase4_security.py`.
- **Residual risk:** page-cache poisoning via shared-cache key collisions is
  an infrastructure concern (`CACHE_MIDDLEWARE_KEY_PREFIX` deployment
  config), not covered by fixtures; owner: platform maintainers (#470).

### Late replies

- **Status: implemented (this change).**
- **Abuse case:** a slow/cancelled provider reply completing after the
  conversation was reset or deleted mid-turn — previously it could attach an
  assistant message to a closed conversation.
- **Control:** `agent_conversation_detail` re-checks conversation active state
  AND existence after `run_turn` and before persisting the assistant message
  (owner-scoped, `active=True`); a closed/deleted conversation yields a
  stable **409 `conversation_closed`** and no assistant message row is
  written. Combined with the stale-patch guard, a late reply can neither
  reapply an obsolete preference change nor attach to a dead conversation.

```mermaid
sequenceDiagram
    participant C as Client
    participant V as agent_conversation_detail
    participant O as Orchestrator
    participant P as Provider gateway
    participant DB as Preference/Conversation store

    C->>V: POST message (idempotency_key)
    V->>DB: persist user turn (get_or_create)
    V->>O: run_turn
    O->>DB: capture preference modified (turn start)
    O->>P: complete(...)
    Note over P: slow reply in flight;<br/>concurrent user edit/reset bumps preference version<br/>or resets/deletes the conversation
    P-->>O: reply with proposed patch
    O->>DB: apply_patch(expected_modified=captured)
    alt preference changed mid-turn
        DB-->>O: StalePreferenceError (not applied)
        O-->>V: PreferenceStaleError
        V-->>C: 409 preference_stale (user turn retryable)
    else version matches
        DB-->>O: applied
        O-->>V: reply
        V->>DB: re-check conversation active+owner
        alt conversation reset/deleted mid-turn
            V-->>C: 409 conversation_closed (no assistant message persisted)
        else still active
            V->>DB: persist assistant message
            V-->>C: 201 reply
        end
    end
```

- **Tests:** `StalePreferenceAndLateReplyTests` (reset and delete mid-turn,
  provider mutates lifecycle state; preferences unchanged; retry works).
- **Residual risk:** the lifecycle re-check is best-effort (read-then-write,
  not serialized with reset/delete); a true serialization point would need a
  per-conversation lock, rejected as an availability regression; owner:
  web/API maintainers.

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
optimistic `modified` check is exercised, the row lock is not.
Backend-specific variants live in
`crank/tests/security/test_mysql_concurrency_variants.py` and are
**skipped unless `CRANK_MYSQL_TEST=1`** with a MySQL-configured settings
target:

```bash
export CRANK_MYSQL_TEST=1
export DJANGO_SETTINGS_MODULE=crank.settings_mysql   # MySQL-configured settings module
export ENV=dev SECRET_KEY=... REDIS_MASTER_URL=redis://localhost:6379/0
# Two concurrent patch writers, one stale: exactly one wins; the stale
# writer receives StalePreferenceError and nothing is overwritten.
python -m pytest crank/tests/security/test_mysql_concurrency_variants.py::MySqlConcurrencyVariants::test_two_writers_one_stale -v
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
