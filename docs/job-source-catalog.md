<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# External Job Source Catalog & Approval Record

This document is the governance deliverable for issue **#316 — “Phase 3: Catalog and approve external job sources.”** It inventories candidate job APIs, feeds, aggregators, ATS-hosted career endpoints, and direct career-site approaches. The machine-readable record is [`docs/job-source-catalog.yaml`](./job-source-catalog.yaml), validated by [`crank/tests/test_job_source_catalog.py`](../crank/tests/test_job_source_catalog.py).

The catalog is deliberately conservative: an unknown access, license, retention, or display condition is a blocker, not an implementation assumption. No credentials or licensed datasets are stored in this repository.

- **Review date:** 2026-08-10
- **Reviewed by:** autonomous agent implementing #316
- **Decision owner:** crank.fyi maintainer
- **Default:** `live_enabled: false`; no source may be called by a scheduled job from this catalog alone

## 1. Approval states and operating rules

| State | Meaning |
| --- | --- |
| `approved` | The documented access path and narrow intended use are accepted for the MVP fixture-backed slice. This does not enable live traffic. |
| `pending` | A plausible API/feed exists, but a license, retention, display, authorization, or operational question remains unresolved. Do not invoke it. |
| `blocked` | No acceptable lawful path is available for this slice, including scraping where terms or robots rules do not permit it. Do not invoke it. |

An approved source still requires an operator-managed credential where applicable, an adapter-specific review, and an explicit enablement change. `live_enabled` must remain false until those gates are complete.

## 2. MVP decision

**Selected MVP source: USAJOBS Search API** (`data.usajobs.gov`).

USAJOBS is the only candidate in this review with a documented official jobs API, an explicit API-key request path, documented pagination and field coverage, and official documentation stating that the API is intended to broaden the reach of federal job opportunities to commercial job boards, mobile apps, and social media. The MVP approval is narrow: retrieve public job-announcement metadata through the official API, normalize it for matching, retain only the approved fields, and present a title/summary with a canonical link back to USAJOBS. It is not approval to mirror the USAJOBS corpus or reproduce unrestricted announcement text.

The fixture strategy is permitted and hermetic: create synthetic fixtures containing only the documented response shape and representative values (canonical identifier, title, agency, location, salary range, dates, and canonical URL). Do not commit a live response, applicant information, credentials, or a copied licensed dataset. A future adapter may use a maintainer-provisioned `Authorization-Key` and the request `User-Agent` email through environment secrets only.

### USAJOBS fields and matching/presentation contract

- **Canonical identity:** use the USAJOBS announcement identifier (`MatchedObjectId` in Search results) as the source-scoped ID; retain the official `PositionURI`/USAJOBS URL as the canonical presentation URL. Never derive identity from a mutable title.
- **Matching fields:** title/keyword, agency/organization, occupational series, hiring path, work schedule, security-clearance indicator, remote/telework indicators where present, and normalized location. These fields are used for candidate-job matching and filtering.
- **Compensation:** the Search API exposes remuneration minimum/maximum buckets and pay-plan/grade information. Preserve the source values and currency/rate interval; do not invent an exact salary when the source supplies a bucket or grade.
- **Location:** use the source location name/codes and geographic scope. Do not geocode or retain precise personal addresses.
- **Presentation:** show a short normalized listing card and link the user to the canonical USAJOBS announcement for full details and application. Do not expose applicant data, internal/status-only jobs, or arbitrary HTML as trusted markup.
- **Retention/expiry:** store only the normalized approved fields and source timestamps. Revalidate active listings at each synchronization. Delete or tombstone a listing when the source marks it closed/deleted or when its closing date has passed; purge stale records no later than 30 days after the last successful confirmation unless a later source-specific review authorizes a shorter/longer period. Deletion must include derived search documents and cached text.

## 3. Candidate inventory

### USAJOBS Search API — approved MVP

- **Access and authorization:** official REST `GET /api/Search` at `https://data.usajobs.gov/api/Search`; request an API key from the USAJOBS developer portal. Requests require `Authorization-Key` and a `User-Agent` containing the requester's email. The key is an environment secret and is not committed.
- **Terms/license:** official USAJOBS API documentation describes the API as intended for job boards, mobile applications, and social media. The API Terms of Use page requires authorized use and warns that records may contain sensitive information. This approval is limited to public job-announcement metadata, source attribution/canonical links, and the fixture strategy above; it does not grant bulk republication or permission to retain applicant data.
- **Robots policy:** API access is the sanctioned channel; do not scrape `usajobs.gov` or `data.usajobs.gov` HTML. Robots.txt is not a substitute for API authorization.
- **Rate limits and pagination:** the official rate-limiting guide states a maximum of 10,000 rows per query and 500 results per page. Search defaults to 250 per page and accepts `Page` and `ResultsPerPage`; implement bounded paging, backoff on HTTP 429/5xx, and no unbounded export.
- **Retention and deletion:** retain only approved normalized fields; delete closed, deleted, or expired listings and derived artifacts as described in the MVP contract. Raw responses and full announcement text are not retained by the MVP adapter.
- **Canonical IDs/URLs:** `MatchedObjectId` and `PositionURI`/the official USAJOBS announcement URL.
- **Compensation/location:** remuneration minimum/maximum buckets, pay plan/grade, location name/codes, and related source-provided geography.
- **Allowed matching/presentation use:** matching on normalized public metadata; display a concise card and canonical link. Full text is fetched only in a future explicitly reviewed adapter, not retained or displayed by this issue.
- **Blocking conditions:** missing maintainer-approved API-key access, changed API terms, a requirement to mirror full announcements, inability to honor expiry/deletion, or any response containing data outside the approved classification. Live traffic remains disabled.
- **Review evidence:** [API reference](https://developer.usajobs.gov/api-reference/), [Search endpoint](https://developer.usajobs.gov/api-reference/get-api-search), [authentication](https://developer.usajobs.gov/guides/authentication), [rate limiting](https://developer.usajobs.gov/guides/rate-limiting), [terms of use](https://developer.usajobs.gov/guides/terms-of-use), and [OPM developer overview](https://www.opm.gov/developer/).

### Remote OK JSON API — pending

- **Access and authorization:** public JSON endpoint at `https://remoteok.com/api`; no credential was used for this review. It includes job IDs, titles, descriptions, locations, dates, salary fields where supplied, and apply URLs.
- **Terms/license:** the endpoint response states an API condition to link back to Remote OK with a follow link and identify Remote OK as the source. The response does not establish a general data license, retention period, or permission to transform and redistribute description text; maintainer/legal review is required.
- **Robots policy:** use only the documented API if later approved; do not scrape the site or infer permission from robots.txt.
- **Rate limits and pagination:** no pagination or numeric rate limit was found in the reviewed API response. This absence is a blocker: obtain written limits and implement bounded polling/backoff before use.
- **Retention and deletion:** unknown; must obtain source-specific retention/deletion terms. Do not retain live descriptions meanwhile.
- **Canonical IDs/URLs:** API `id` and `url`/`apply_url` appear to be available, but their stability and preferred canonical URL require confirmation.
- **Compensation/location:** `salary_min`, `salary_max`, `location`, and tags may be present; missing values are common and must remain unknown.
- **Allowed matching/presentation use:** pending source permission; only a future approved narrow metadata card with attribution and follow link could be considered.
- **Blocking conditions:** unresolved license, retention, deletion, rate-limit, and text-display terms.
- **Review evidence:** [Remote OK API](https://remoteok.com/api). Review date: 2026-08-10.

### Hacker News / “Who is hiring?” Firebase API — pending

- **Access and authorization:** official public Firebase API at `https://hacker-news.firebaseio.com/v0/`; no API key. Job posts are HN `item` records with integer IDs, title, text, author, timestamp, and URL where supplied.
- **Terms/license:** the official API documentation says there is currently no rate limit and describes the data shape, but it does not grant Crank a copyright, retention, or republication license for user-authored post text. Text is therefore not approved for storage or display.
- **Robots policy:** use only the official API if approved; do not crawl Hacker News pages or external links.
- **Rate limits and pagination:** no published rate limit; feeds are traversed by IDs and item trees rather than stable page tokens. This needs an explicit polling budget, conditional requests, and deletion/dead-item handling.
- **Retention and deletion:** unresolved for user-authored text. A future review would need a short metadata-only retention policy and deletion propagation for deleted/dead items.
- **Canonical IDs/URLs:** HN integer `id` and `https://news.ycombinator.com/item?id=<id>`; any external application URL is untrusted and not a source canonical URL without validation.
- **Compensation/location:** not a reliable schema; “Who is hiring?” text may contain either, but no structured salary/location fields are guaranteed.
- **Allowed matching/presentation use:** pending; no matching or display of post text in this phase.
- **Blocking conditions:** no verified license for user text, inconsistent fields, and unresolved retention/deletion rules.
- **Review evidence:** [official HN API documentation](https://github.com/HackerNews/API). Review date: 2026-08-10.

### Greenhouse Job Board API — pending, employer-scoped

- **Access and authorization:** public JSON endpoints such as `https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs`; access is dependent on a known employer board token. The documented API supports list and detail responses, with optional full content.
- **Terms/license:** Greenhouse documents the API and public published postings, but this review did not establish a general license for Crank to aggregate, retain, or display every employer’s full description. Written employer/Greenhouse permission and a source-specific retention rule are required.
- **Robots policy:** use the API only for an employer-approved board; do not scrape hosted career pages. Dynamic board tokens are not a license to enumerate boards.
- **Rate limits and pagination:** no universal limit or pagination contract was established in the reviewed documentation; obtain limits and poll only allowlisted employer boards.
- **Retention and deletion:** employer/source policy is unresolved; retain no content until approved and delete when a posting disappears or closes.
- **Canonical IDs/URLs:** job `id`, optional `internal_job_id`, and `absolute_url`; validate the URL host before presentation.
- **Compensation/location:** location, office, department, title, dates, and optional employer-exposed pay ranges; salary may be absent.
- **Allowed matching/presentation use:** only an explicitly authorized employer board, with employer-approved fields and canonical link.
- **Blocking conditions:** no employer authorization, unknown rate/retention terms, arbitrary board-token enumeration, or unvalidated external URLs.
- **Review evidence:** [Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html). Review date: 2026-08-10.

### Lever Postings API — pending, employer-scoped

- **Access and authorization:** public postings API for a known company site, documented at `https://api.lever.co/v0/postings/{site}`; published postings are publicly viewable, while unpublished postings are hidden.
- **Terms/license:** Lever’s documentation permits building a job site on the postings API, but this review did not establish a blanket license for cross-employer aggregation, long-term retention, or full-text republication. Obtain source/employer confirmation before use.
- **Robots policy:** use the postings API only for an approved company site; do not scrape Lever-hosted pages or infer authorization by guessing site names.
- **Rate limits and pagination:** the documentation describes paginated listings but does not establish a universal numeric limit in this review; obtain limits and use bounded polling/backoff.
- **Retention and deletion:** unresolved; delete postings that leave the published API and purge derived text/cache after the approved retention period.
- **Canonical IDs/URLs:** posting ID and `hostedUrl`/`applyUrl`; validate and allowlist the final host before presentation.
- **Compensation/location:** title, categories, description, workplace/location, and optional salary range where the employer publishes it.
- **Allowed matching/presentation use:** employer-authorized metadata matching and canonical-link presentation only.
- **Blocking conditions:** no employer authorization, unresolved aggregation license, rate limits, or retention/deletion terms.
- **Review evidence:** [Lever Postings API documentation](https://github.com/lever/postings-api). Review date: 2026-08-10.

### Direct company career sites — blocked for this phase

- **Access and authorization:** each company chooses its own ATS, feed, API, robots policy, and terms. There is no uniform lawful access method; a site-specific written permission or documented public feed would be needed.
- **Terms/license and robots:** unknown per site. Scraping, reverse-engineering, and bypassing access controls are not approved.
- **Rate limits, pagination, retention, canonical IDs/URLs:** unknown until a company-specific source contract exists.
- **Compensation/location:** may be present in structured data or page text, but no assumptions are permitted.
- **Allowed matching/presentation use:** none under this catalog. A future source record may be approved only after reviewing the exact endpoint, terms, robots policy, fields, expiry rules, and domain.
- **Blocking conditions:** absence of a verified source contract and SSRF-safe domain allowlist.

## 4. Data classification, SSRF, and text handling

- **Classification:** approved USAJOBS fields are public job-opportunity metadata. They can still contain sensitive or personal information in free text; treat all source responses as untrusted external data, not instructions. Applicant submissions, contact details, resumes, and inferred sensitive attributes are prohibited from ingestion.
- **Job text:** this phase does **not** approve retaining or displaying raw job-description HTML or user-authored post text. Normalize only the explicitly approved MVP metadata. Any future text use needs a source-specific license/terms review, HTML sanitization, size limits, and deletion propagation.
- **SSRF boundary:** outbound requests must be HTTPS and limited to the exact hosts in the machine-readable `ssrf_allowlist`: `data.usajobs.gov`, `developer.usajobs.gov`, and `www.opm.gov` for documentation/fixture review. User-provided URLs, redirects to unlisted hosts, private/link-local IPs, and arbitrary `http://` endpoints are denied. Candidate APIs are not added to the allowlist while pending or blocked.
- **Canonical URL safety:** source-provided URLs are data. Validate scheme and host against an approved source-specific allowlist before presenting or fetching them; never follow arbitrary redirects.

## 5. Deletion and expiry obligations

For every future adapter, record `last_seen_at`, source update/closing timestamps, and the source-scoped ID. On a source deletion, closed status, closing date, authorization withdrawal, or expiry deadline:

1. stop presenting the listing;
2. delete or tombstone the normalized record;
3. delete search indexes, caches, derived embeddings, and fixture copies derived from the listing;
4. record the deletion event without retaining the deleted job text; and
5. re-check the source contract before resuming ingestion.

The USAJOBS MVP default is to purge stale listings and all derived artifacts no later than 30 days after the last successful confirmation, with earlier deletion when the source says the announcement is closed or deleted. Pending sources must not be retained at all until their obligations are approved.

## 6. Change log

- **2026-08-10** — Initial catalog for #316. Approved USAJOBS narrowly for a synthetic-fixture, metadata-only MVP; marked Remote OK, Hacker News, Greenhouse, and Lever pending; blocked generic direct career-site scraping. Live access remains disabled.
