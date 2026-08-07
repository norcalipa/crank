<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Agentic Capabilities Roadmap for crank.fyi

## 1. Purpose

This document defines an implementation-ready roadmap for two related but operationally distinct capabilities:

1. An interactive job search agent that learns an authenticated user's preferences and recommends organizations.
2. Scheduled ingestion workflows that refresh external organization scores and find matching job listings.

Progress is tracked through GitHub issues in the crank.fyi project. Each issue must identify its phase, dependencies, acceptance criteria, test expectations, and operational or security requirements.

## 2. Goals, MVP, and Non-Goals

### 2.1. MVP goals

- Interview authenticated users about compensation, culture, work location, funding stage, vesting, geography, industry, and other job criteria.
- Store validated, structured preferences for deterministic filtering while maintaining a human-readable markdown summary for prompts and user review.
- Recommend organizations using existing crank.fyi data and explain why each recommendation matches.
- Ingest scores from one approved external rating source through an extensible adapter pipeline.
- Ingest listings from one approved job source, associate known employers with `Organization` records, and persist ranked matches.
- Run ingestion as observable, idempotent Django management commands scheduled by Kubernetes.
- Give users control over their preference data and prevent users from accessing one another's preferences or matches.

### 2.2. Non-goals for the MVP

- Applying to jobs or contacting employers on a user's behalf.
- Automatically creating organizations from untrusted external data.
- Email, SMS, or push notifications.
- Supporting every rating site or job board before validating one vertical slice.
- Training or fine-tuning a model on user preference data.
- Autonomous browsing outside an explicitly approved source allowlist.
- Multi-language support.

## 3. Current Codebase Constraints

The implementation must build on the repository's existing conventions:

- Django, Django REST Framework, django-allauth, MySQL, React, and TypeScript are already in use.
- `Organization.gives_ratings` identifies organizations that may be score sources.
- `Score.source` is a rating organization, `Score.target` is the rated organization, and `Score.type` is a `ScoreType`.
- `Score` inherits `ActivatorModel` and permits only one active row for a `(type, source, target)` tuple. Score ingestion must honor this partial uniqueness constraint.
- `Organization.avg_scores()` caches computed values. Score writes must invalidate all affected organization and algorithm result caches.
- The existing Redis instance is a small, non-persistent cache. It must not be repurposed as a durable task broker.
- The production runtime is Kubernetes. No Celery workers, scheduler, LLM client, browser runtime, or agent framework currently exists.
- Authentication is provided by django-allauth. Preference and match APIs must require an authenticated user and enforce object ownership.

## 4. Architecture Decisions

These decisions remove major implementation ambiguities. Revisit them only through a documented architecture change.

| Area | Initial decision | Rationale |
|---|---|---|
| LLM integration | A small provider-neutral Python interface with one configured provider | Keeps application services testable and avoids coupling to an agent framework |
| Agent framework | Direct prompt orchestration and structured outputs | The initial flow does not justify LangChain or another orchestration dependency |
| Preference storage | Versioned `JSONField` as canonical data plus generated markdown | Structured data supports deterministic matching; markdown remains readable and useful in prompts |
| Conversation state | Persist minimal conversation sessions/messages with retention controls | Supports multi-turn flows without trusting client-supplied history |
| Scheduling | Idempotent Django management commands in Kubernetes `CronJob` resources | Fits the existing deployment and avoids a new worker/broker topology |
| Source integrations | Typed adapters using approved APIs or direct HTTP first; Playwright library only when permitted and necessary | Keeps production dependencies explicit and testable |
| MCP | Development tooling only, not a production runtime dependency | MCP servers available to coding agents are not application infrastructure |
| Score updates | Transactionally deactivate the previous active score and create a new active observation | Preserves history while respecting the existing active-row constraint |
| Unknown employers | Record as unresolved; do not create `Organization` rows automatically | Prevents untrusted sources from mutating the organization catalog |
| Notifications | Authenticated in-app presentation for MVP | Email and other channels require separate consent and delivery infrastructure |
| Observability | Structured logs, persisted run summaries, and New Relic events/alerts | Builds on the repository's existing New Relic integration |

Provider and source credentials must be supplied through environment-backed secrets. Secrets, raw credentials, and complete external responses must not be logged or stored in source control.

## 5. Logical Components and Data

### 5.1. Interactive job search agent

The authenticated chat endpoint delegates to an application service rather than calling an LLM directly from a view.

1. Load the user's canonical preference document.
2. Load only the conversation context allowed by the retention policy.
3. Supply existing organization and score data through bounded, server-controlled tools.
4. Request a schema-validated response containing an assistant message and optional preference patch.
5. Validate and transactionally apply the patch.
6. Return the response and disclose whether preferences changed.

The model must never generate SQL or select arbitrary URLs. Recommendations must reference organization IDs returned by server-side queries.

### 5.2. Preference model

The implementation should follow existing model conventions while keeping these logical fields:

- `user`: one-to-one relation to `settings.AUTH_USER_MODEL`.
- `preferences`: versioned JSON object and canonical source of truth.
- `preferences_markdown`: server-generated human-readable projection.
- `schema_version`: integer used for migrations and validation.
- created/modified timestamps.

The JSON schema should distinguish required preferences, optional preferences, exclusions, and free-form notes. Updates are typed patches with explicit replacement/removal semantics; the LLM must not rewrite an unchecked markdown blob.

Create preferences on first agent interaction, not automatically at OAuth login. Users must be able to view, reset, export, and delete their preferences.

### 5.3. Conversation data

Persist a conversation/session record and ordered messages sufficient to support multi-turn interactions. Define retention and deletion behavior before enabling production traffic. Do not store provider reasoning, hidden prompts, API credentials, or unnecessary copies of sensitive profile data.

### 5.4. Source and run records

Use a source catalog for rating and job integrations. Each source records:

- Source type, linked rating `Organization` where applicable, base URL, adapter key, and enabled state.
- Approved access method and terms/robots review status.
- Request cadence, timeout, rate limit, and data retention policy.
- Supported score types or job data capabilities.

Persist an `AgentRun` summary for each scheduled execution with a run type, status, timestamps, counts, and sanitized error summary. A run is successful only after its writes commit.

### 5.5. Score observations and provenance

Every normalized score must retain enough provenance to audit the write:

- Source URL or stable external identifier.
- Observed and fetched timestamps.
- Raw value and normalized value/range.
- Adapter/version identifier and confidence or validation status.
- Owning run.

For each normalized `(type, source, target)` observation, lock the current active row, deactivate it, and create the replacement in one transaction. Identical observations should be recognized as no-ops. After a committed change, invalidate affected organization detail, organization score, average score, and algorithm result cache keys.

### 5.6. Job listings and matches

Persist normalized job listings with a stable source identifier, canonical URL, employer text, title, location, compensation when available, first/last seen timestamps, and active state. Deduplicate by source identifier or canonical URL.

Resolve employers against active `Organization` rows using normalized names, domains, and curated aliases. Unresolved employers are recorded for review and excluded from user matches.

Persist user-specific matches separately from listings, including rank, rationale, criteria version, first/last matched timestamps, and seen/dismissed state. Matching uses structured preferences; an LLM may explain a result but must not be the only filter or ranking mechanism.

### 5.7. LLM provider gateway (`crank/agents/llm.py`)

The gateway is a small, provider-neutral interface for schema-capable LLM
completions with a single concrete provider selected through settings. It is

the only agent layer in this deliverable; there is no agent framework.

- **Protocol, not framework.** Call sites depend on the `LLMProvider` protocol
  and `LLMResult`/`LLMUsage` data types. Provider SDK calls live only inside
  adapter implementations, so no provider SDK is imported at a call site and
  no network I/O happens at module import.
- **Environment-backed, fail-closed settings.** `LLM_PROVIDER`, `LLM_MODEL`,
  `LLM_TIMEOUT_SECONDS`, `LLM_MAX_TOKENS`, `LLM_PER_USER_COST_LIMIT_USD`,
  `LLM_PRICE_PER_1K_TOKENS_USD`, and `INTERACTIVE_AGENT_ENABLED` are read from
  the environment in `crank/settings/base.py`. `LLM_API_KEY` is the only
  secret, read solely from an environment secret with no checked-in default.
  Missing or invalid provider configuration raises `LLMConfigurationError`
  before any request is sent.
- **Offline default.** `crank.agents.llm:FakeLLMProvider` is the selected
  implementation and makes no network calls, so a live provider is never
  contacted until a real adapter plus key are deployed.
- **Ceilings and usage.** Timeout, token, and cost ceilings are enforced before
  a request is sent; usage is returned provider-neutrally as `LLMUsage`
  (`prompt/completion/total_tokens`, `cost_estimate_usd`) with latency.
- **Independent feature flag.** `INTERACTIVE_AGENT_ENABLED` gates interactive
  agent execution independently of scheduled ingestion, which has its own
  lifecycle controls.
- **Local test config (FAKE credentials only).** For local runs set
  `LLM_PROVIDER=crank.agents.llm:FakeLLMProvider` and leave `LLM_API_KEY`
  empty; the fake requires no key and makes no network calls. Never commit a
  real API key.

## 6. Source Access and Safety Policy
- Complete a source review before implementation: API availability, license/terms, robots policy, authentication, rate limits, retention limits, and allowed use.
- Prefer official APIs and feeds. Use direct HTTP only where access is permitted.
- Use Playwright as an application library only for approved sources that require browser rendering.
- Do not imply that Glassdoor, LinkedIn, or another named service provides a generally available API until access is verified.
- Enforce HTTPS, domain allowlists, redirect limits, response size limits, timeouts, content-type checks, and private-network blocking to mitigate SSRF.
- Treat all scraped content as untrusted data. It cannot modify prompts, tool policy, source configuration, or system instructions.
- Respect robots directives and source-specific rate limits. A source marked blocked or pending review must not run.

## 7. Reliability, Privacy, and Cost Controls

- Commands and adapters must be idempotent, resumable at a source boundary, and safe under overlapping scheduler invocations.
- Use bounded retries with exponential backoff and jitter only for transient failures.
- Apply per-run, per-user, and global LLM token/cost limits. Record usage without storing prompt content in metrics.
- Redact secrets and sensitive user content from logs, traces, New Relic events, and error messages.
- Require owner-scoped queries for preferences, conversations, and matches. Admin access must be auditable.
- Provide preference and conversation export/deletion paths and document retention periods.
- Validate model output against an explicit schema before it reaches persistence or source adapters.
- Put interactive and scheduled capabilities behind separate feature flags with a kill switch for each source.

## 8. Implementation Phases

Issues are the executable specification. The phase checklists below describe scope and completion gates rather than replacing issue-level acceptance criteria.

### 8.1. Phase 1: Foundation

**Implementation scope**

- [ ] Establish agent configuration, feature flags, provider interface, timeouts, and cost controls.
- [ ] Add preference, conversation, and message models with migrations, ownership rules, retention behavior, and admin support.
- [ ] Implement schema validation, typed preference patches, markdown projection, and export/reset/delete services.
- [ ] Implement the prompt, bounded organization-data tools, and provider-independent conversation orchestration.
- [ ] Add authenticated chat APIs and a minimal accessible React conversation interface.
- [ ] Add management-command conventions, run locking, `AgentRun` persistence, and a disabled-by-default Kubernetes `CronJob` foundation.

**Exit criteria**

- An authenticated user can complete a mocked end-to-end conversation and persist validated preferences.
- A user cannot read or mutate another user's data.
- Provider calls, preference changes, failures, and cost limits are covered by automated tests without network access.
- A no-op scheduled command runs safely, records a run, and prevents overlapping execution.

### 8.2. Phase 2: Score Gathering

**Implementation scope**

- [ ] Catalog and approve candidate rating sources before writing adapters.
- [ ] Implement source/run/provenance models and typed adapter contracts.
- [ ] Build one API/direct-HTTP source adapter as a vertical slice using recorded fixtures.
- [ ] Normalize score types, values, ranges, source identities, and target organizations.
- [ ] Implement transactional, idempotent active-score replacement and cache invalidation.
- [ ] Schedule score gathering and add per-source metrics, sanitized failures, retries, and alerts.

**Exit criteria**

- One approved source completes a scheduled fixture-backed ingestion into a staging environment.
- Reprocessing the same observation creates no duplicate active score or unnecessary history.
- A changed observation preserves provenance/history and immediately updates cached views.
- One failing source does not prevent approved independent sources from completing.

### 8.3. Phase 3: Job Matching

**Implementation scope**

- [ ] Catalog and approve candidate job sources.
- [ ] Implement job source adapters and normalized listing persistence/deduplication.
- [ ] Resolve employers against known active organizations and queue unresolved names for review.
- [ ] Build deterministic filtering/ranking from versioned structured preferences.
- [ ] Persist user matches and expose owner-scoped in-app match APIs/UI.
- [ ] Schedule periodic matching with source/user batching, limits, metrics, and partial-failure handling.

**Exit criteria**

- One approved source ingests fixture-backed listings without duplicates.
- Matching is repeatable for a fixed listing and preference version.
- Unknown employers never create organizations or user matches automatically.
- Users can view only their own ranked, explained matches.

### 8.4. Phase 4: Hardening and Rollout

**Implementation scope**

- [ ] Add end-to-end tests for interactive and scheduled flows, including authorization, prompt injection, SSRF, and privacy controls.
- [ ] Test scheduler overlap, retries, partial failures, source throttling, volume, and run-window performance.
- [ ] Configure New Relic dashboards/alerts and admin run/source controls.
- [ ] Complete accessibility and user acceptance testing.
- [ ] Publish user privacy/help documentation and operator runbooks.
- [ ] Roll out behind feature flags with explicit staging and production success/rollback gates.

**Exit criteria**

- CI covers critical success, failure, authorization, and security paths.
- Representative scheduled workloads complete within the configured window and resource limits.
- Operators can disable a source or capability, inspect failures, and identify the last successful run.
- Users understand what is stored and can export or delete their agent data.
- Production rollout and rollback owners approve the release.

## 9. Delivery Dependencies

- Phase 2 and Phase 3 depend on the Phase 1 run/scheduling foundation.
- Phase 2 adapter work depends on source approval and the source contract.
- Phase 3 matching depends on canonical structured preferences, listing persistence, and organization resolution.
- Phase 4 starts incrementally, but production rollout depends on all prior phase exit criteria.
- Provider, model, source, schedule, and retention configuration must be environment-driven and disabled by default until secrets and policies are ready.

## 10. Future Considerations

- Opt-in email or push notifications.
- Additional rating and job sources after each source passes review.
- A dedicated visual preference editor.
- User feedback signals for recommendation quality.
- Multi-language prompts and preference schemas.
- Separate workers or a durable queue if measured workload exceeds Kubernetes `CronJob` limits.

---

**Status:** Architecture refined; implementation tracked in the project backlog.
