<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Threat model: agentic job-search features

Review date: 2026-08-12
Scope: authenticated chat, preferences, score/source ingestion, listing
resolution, deterministic matching, and owner-scoped match presentation.

This review is intentionally bounded to the existing agentic feature paths. It
covers the offline security/integration suite in
`crank/tests/security/test_phase4_security.py` and the focused boundary tests
listed below. No live provider or source traffic is required.

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
requests cannot follow unapproved/private destinations, and sensitive values do
not appear in logs/events. Residual risks above are operational or require
separate production/load/legal review; they are not widened by this phase.
