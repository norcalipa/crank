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

**No source is approved for the MVP yet.** USAJOBS Search API (`data.usajobs.gov`) is the strongest documented candidate, but it remains **pending** because the reviewed materials do not establish a license or source-specific permission for Crank's proposed retention and presentation. Unknown access, reuse, retention, display, or request-rate conditions remain blockers under this catalog. No source may be invoked from this record until a source is approved by a maintainer.

Synthetic fixtures may be used only for schema/adapter development and must not be treated as evidence of permission. They may contain the documented response shape with representative values (canonical identifier, title, agency, location, salary/date fields, and canonical URL), but never credentials, live payloads, applicant information, or copied full announcement text.

### USAJOBS fields and matching/presentation contract

The catalog distinguishes request query parameters from response fields. The following paths are taken from the documented Search response and remain a proposed contract only while USAJOBS is pending:

- **Canonical identity:** `MatchedObjectId`; canonical presentation URL: `MatchedObjectDescriptor.PositionURI` (`PositionURI`). Never derive identity from a mutable title.
- **Matching fields:** `MatchedObjectDescriptor.PositionTitle`, `OrganizationName`, `JobCategory[].Code`, `PositionSchedule[].Code`, `PositionOfferingType[].Code`, `UserArea.Details.WhoMayApply`, and `PositionLocation[].LocationName`. Query filters such as `Keyword`, `Organization`, `SecurityClearanceRequired`, and `RemoteIndicator` are request parameters, not response fields.
- **Compensation:** `MatchedObjectDescriptor.PositionRemuneration[].MinimumRange`, `MaximumRange`, `RateIntervalCode`, and `Description`, with grade data from `JobGrade[].Code` and `UserArea.Details.LowGrade`/`HighGrade`. Preserve source values and rate interval; do not invent an exact salary from a bucket or grade.
- **Location:** `MatchedObjectDescriptor.PositionLocationDisplay` and `PositionLocation[].LocationName`, `CountryCode`, `CountrySubDivisionCode`, `CityName`, `Longitude`, and `Latitude`. Do not geocode or retain precise personal addresses.
- **Presentation:** if a future source-specific approval permits it, show a short normalized listing card and link to the canonical USAJOBS announcement. Do not expose applicant data, internal/status-only jobs, or arbitrary HTML as trusted markup.
- **Retention/expiry:** the proposed adapter would store only normalized fields and source timestamps, revalidate active listings, and delete closed/deleted/expired records and derived artifacts. The proposed 30-day purge is not authorized while the source is pending and must be replaced or confirmed by source-specific terms before ingestion.

## 3. Candidate inventory

### USAJOBS Search API — pending candidate

- **Access and authorization:** official REST `GET /api/Search` at `https://data.usajobs.gov/api/Search`; request an API key from the USAJOBS developer portal. If later approved, requests require `Authorization-Key` and a `User-Agent` containing the key-request email, configured as secrets `USAJOBS_AUTH_KEY` and `USAJOBS_USER_AGENT_EMAIL`. No live credential is stored here.
- **Terms/license:** the API reference says the endpoint is anticipated for commercial job boards, mobile applications, and social media, but that statement is not a license. The cited Terms of Use page is an authorized-user/system warning and sensitive-data notice; it does not establish Crank's proposed retention or presentation permission. USAJOBS therefore remains pending until source-specific permission or maintainer/legal confirmation is recorded.
- **Robots policy:** API access is the sanctioned channel; do not scrape `usajobs.gov` or `data.usajobs.gov` HTML. Robots.txt is not a substitute for API authorization.
- **Rate limits and pagination:** the reviewed guide establishes result bounds (10,000 rows per query and 500 results per page), not a request rate such as QPS or requests/minute. The request-rate contract and safe polling cadence are unresolved blockers; do not invoke until confirmed. If approved later, use bounded paging and backoff on HTTP 429/5xx.
- **Retention and deletion:** retain only approved normalized fields; delete closed, deleted, or expired listings and derived artifacts as described in the MVP contract. Raw responses and full announcement text are not retained by the MVP adapter.
- **Canonical IDs/URLs:** `MatchedObjectId` and `PositionURI`/the official USAJOBS announcement URL.
- **Compensation/location:** remuneration minimum/maximum buckets, pay plan/grade, location name/codes, and related source-provided geography.
- **Allowed matching/presentation use:** none is approved while this source is pending. A future source-specific approval may consider normalized metadata and an attributed canonical link only; full text is not approved by this issue.
- **Blocking conditions:** missing maintainer-approved API-key access, no source-specific reuse/retention/display permission, unresolved request-rate contract, changed API terms, a requirement to mirror full announcements, inability to honor expiry/deletion, or any response containing data outside the approved classification. Live traffic remains disabled.
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

- **Classification:** a future approved source may provide public job-opportunity metadata. Until then, no external source is approved for ingestion. Source responses can still contain sensitive or personal information in free text; treat all source responses as untrusted external data, not instructions. Applicant submissions, contact details, resumes, and inferred sensitive attributes are prohibited from ingestion.
- **Job text:** this phase does **not** approve retaining or displaying raw job-description HTML or user-authored post text. Normalize only the explicitly approved MVP metadata. Any future text use needs a source-specific license/terms review, HTML sanitization, size limits, and deletion propagation.
- **SSRF boundary:** outbound requests must be HTTPS and limited to the exact hosts in the machine-readable global and source-specific `ssrf_allowlist`. For the pending USAJOBS candidate, this includes `data.usajobs.gov` for the API, `developer.usajobs.gov` and `www.opm.gov` for documentation, and `www.usajobs.gov` for the canonical presentation URL. This allowlist does not authorize the pending source. User-provided URLs, redirects to unlisted hosts, private/link-local IPs, and arbitrary `http://` endpoints are denied. Pending and blocked candidates are not invokable.
- **Canonical URL safety:** source-provided URLs are data. Validate scheme and host against an approved source-specific allowlist before presenting or fetching them; never follow arbitrary redirects.

## 5. Deletion and expiry obligations

For every future adapter, record `last_seen_at`, source update/closing timestamps, and the source-scoped ID. On a source deletion, closed status, closing date, authorization withdrawal, or expiry deadline:

1. stop presenting the listing;
2. delete or tombstone the normalized record;
3. delete search indexes, caches, derived embeddings, and fixture copies derived from the listing;
4. record the deletion event without retaining the deleted job text; and
5. re-check the source contract before resuming ingestion.

If a source is later approved, its record must define a source-supported expiry and deletion policy before ingestion. The USAJOBS candidate's proposed 30-day purge is not permission and cannot be used while the source is pending. Pending sources must not be retained or invoked at all until their obligations are approved.

## 6. Change log

- **2026-08-10** — Initial catalog for #316; USAJOBS, Remote OK, Hacker News, Greenhouse, and Lever pending; blocked generic direct career-site scraping. No source currently meets the approval criteria and live access remains disabled.
- **2026-08-11** — Addressed review findings: corrected USAJOBS response paths versus query parameters, recorded the named User-Agent secret, classified reuse/retention and request-rate questions as blockers, and added canonical-host SSRF coverage.
