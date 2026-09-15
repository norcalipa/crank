<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Publication outbox (issue #470)

## Decision: ADOPT the transactional outbox

Accepted data changes (active scores, company profile observations, ingested
listing sources) record a `crank_publicationevent` row **inside the same
database transaction as the change itself**. The row is the transactional
outbox the issue asked to assess.

Why adopt: the previous invalidation mechanism,
`transaction.on_commit(invalidate)`, is in-process and in-memory. If the
worker dies between the database commit and callback dispatch, the callback is
lost and every affected cache stays stale until TTL expiry (60s by default)
with **no durable record that publication work was required** — exactly the
failure mode the issue describes. The outbox row commits atomically with the
data, so it closes the commit→dispatch loss: a crash at any point before the
sweep runs leaves a durable pending event that the next sweep processes.

Rejected alternatives (see the issue plan for full reasoning):

- *Do nothing / rely on `on_commit` + TTL* — callbacks are lost on worker
  crash; no durable record of required recomputation.
- *Redis Streams / pub-sub as the durable record* — the issue forbids Redis
  as the durable record of accepted data or required work; MySQL is the
  system of record.
- *Per-listing events* — unbounded row growth from ingestion; per-source
  events carry the same invalidation information.

## Data model

`PublicationEvent` (`crank_publicationevent`, migration
`0031_publicationevent`, parent `0029_merge_20260815_1645`):

- `id` — auto-increment PK; **the data revision identity**.
- `target_type` — `organization` | `score` | `listing`.
- `target_id` — indexed integer id of the changed row (no FK; events survive
  target deletion). Indexed via `(target_type, target_id)`
  (`crank_pubevent_target_idx`) so revision lookups by target (#462
  consumers) never scan the outbox.
- `event_kind` — `created` | `changed` | `observed` | `ingested`.
- `payload` — bounded, allowlisted JSON (`score_type_id`, `source_id`,
  `observation_id`, counters, `chunk_index`/`chunk_count`, and
  `organization_ids` chunked at ≤200 per event). Payload lists are bounded
  by construction and never silently truncated: a writer that needs more ids
  emits multiple chunk events, and an oversized list fails loudly instead of
  dropping invalidations. No user content, no credentials; strings are
  truncated.
- `created_at`, `processed_at` (null until swept; indexed, plus a
  `(processed_at, id)` index for the sweep query).

Writers (all in the same transaction as the accepted change):

- `crank/services/scores.py` `_persist_locked` — one event per
  `created`/`changed` observation; none for `noop`.
- `crank/services/company_crawler.py` — one organization event per
  `CompanyProfileObservation` create with a resolved organization (any
  non-rejected observation changes the public provenance payload).
- `crank/services/job_pipeline.py` — listing events for each source stage,
  recorded **inside the same transaction as the source's accepted writes**:
  the events commit exactly when the writes commit, an outbox insert failure
  (or any crash in the block) rolls the writes back, and a source with
  partial row failures still publishes the rows it accepted. The payload
  carries the source's deduplicated employer organization ids split into
  bounded chunks (`chunk_index`/`chunk_count`), so every organization is
  published even when a source maps to more organizations than one payload
  chunk holds.

## Consumer

`crank/services/publication.py` `sweep_pending(limit)`, invoked by the
`publication_sweep` management command on a bounded cron schedule.
**Consumer owner: operations cron.**

Per sweep, bounded by `--limit` (default `PUBLICATION_SWEEP_BATCH_SIZE`,
500):

1. Select pending events (`processed_at IS NULL`) ordered by `id`.
2. Union the affected cache keys across **every** selected event — pending
   events for the same `(target_type, target_id)` can carry different
   affected-key sets (score events with different `score_type_id`s weight
   different algorithms; chunked listing events carry different
   organization ids), so collapsing to the latest payload would drop
   required invalidations.
3. Delete the deduplicated key set **once** (keys computed by
   `scores.affected_cache_keys`, the centralized function, which includes
   the `organization_provenance_api_{pk}` key and both the
   `algorithm_{id}_results` key and the full-page `algorithm_{id}_page`
   shell key backing the `/algo/<id>/` cached view).
4. Mark the swept rows processed with a conditional
   `processed_at IS NULL` update.

Ordering and idempotence make it crash-safe: keys are deleted *before*
`processed_at` is set, so a crash at any point only ever re-runs idempotent
deletions, and a concurrent sweep can neither double-count nor un-process
rows. Re-running the sweep is always safe. The `transaction.on_commit`
invalidation remains in place as the best-effort freshness fast path; the
sweep is the durability backstop, not a replacement.

## Configuration and rollout

- `PUBLICATION_CONSUMER_ENABLED` (default **False**) and the
  `publication_consumer` `CapabilitySwitch` gate the consumer; either alone
  stops it, and pending events simply accumulate (data preserved) while it is
  off. The switch is exercised by `rollback_drill`.
- Rollout order: (1) migrate — additive table, old code ignores it; (2) deploy
  writer code — events recorded, consumer off; (3) enable
  `PUBLICATION_CONSUMER_ENABLED`; (4) wire the cron.
- Rollback order: disable the `publication_consumer` switch (or the env
  flag); pending events accumulate; re-enabling resumes from where the sweep
  left off. All code is compatible with or without the table; cache-key
  behavior only ever becomes *more* invalidation (the provenance key), never
  less.

## Testing and residual risk

CI runs SQLite (`ENV=dev python -m pytest crank/tests/services/test_publication.py`
and the full suite) and `python manage.py makemigrations --check --dry-run`.
The MySQL variants (row-lock concurrency, conditional-update behavior under
real contention) are the same suite run against the MySQL settings target:

```sh
ENV=dev DJANGO_SETTINGS_MODULE=crank.settings \
  python -m pytest crank/tests/services/test_publication.py -v
```

Fixture runs alone are not a production proof — recorded as residual risk.
Concurrency-critical paths avoid backend-specific features (no
`SKIP LOCKED`, no partial unique constraints), so the SQLite suite exercises
the same logic; the residual risk is unobserved MySQL-specific plan or lock
behavior, mitigated by the conditional-update design and bounded sweep.
