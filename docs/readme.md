# Plan for Agentic Capabilities on crank.fyi

This document outlines a plan to integrate agentic capabilities into the crank.fyi platform. crank.fyi is a Django-based application that tracks organizational scores (ratings) from various sources against companies and organizations.

**Progress tracking:** Managed via GitHub Issues and Projects for a dynamic dashboard experience.

## 1. Feature Overview

### 1.1. Job Search Agent

An interactive conversational agent that helps users find the best companies to work for based on their personal criteria.

**Core prompt description:**
> You are a career advisor agent for crank.fyi. Your job is to help users find the best companies for them to work for based on their unique criteria. Interview the user to understand their preferences — compensation expectations, culture, remote/hybrid/in-office preference, funding stage, vesting, location, industry, and any other factors that matter to them. Use the scores and organization data available in the system to recommend companies that match their criteria. Reference the user's stored preferences (in markdown format) to personalize recommendations over time. When the user has new information or changes their mind, update their stored preferences accordingly.

**Per-user memory:**
- A new database table `UserPreference` will be created to store user preferences in markdown format.
- Each row stores preferences for a single user, keyed by user ID.
- When the user interacts with the job search agent, their answers update their markdown-formatted preference document.
- The agent reads the markdown preferences at the start of each session to provide personalized recommendations.
- Preferences are updated incrementally as the user provides new information during conversations.

**Database schema (new model):**
```python
class UserPreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    preferences_markdown = models.TextField(
        default="",
        help_text="User's job preferences stored in markdown format"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

### 1.2. Cron-Based Web Search Agent

A scheduled background agent that periodically gathers updated data from external sources.

**Responsibilities:**

**A. Score Gathering:**
- For each `ScoreType` defined in the system, crawl web sources to find updated scores.
- Targets all organizations where `Organization.gives_ratings == True` (these are the sources that provide ratings).
- For each source organization, find the latest scores it has published for each `ScoreType` against all target organizations.
- Update the `Score` records in the database with the new values.

**B. Job Matching:**
- For each user who has stored preferences in `UserPreference`, search for open job listings that match their criteria.
- Use the user's markdown-formatted preferences (compensation, location, RTO policy, industry, etc.) to filter and rank results.
- Matches are cross-referenced against the `Organization` table to ensure the hiring company is known in the system.
- Results can be presented to the user or stored for later retrieval.

**Web search implementation:**
- Use web scraping tools (Firecrawl MCP server, Playwright MCP server, or direct HTTP) to navigate rating sites, review platforms, and job boards.
- Where APIs exist (e.g., Glassdoor, LinkedIn, company career portals), use them as a primary source.
- Fall back to scraping for sites that don't offer APIs.
- The cron job runs on a configurable schedule (e.g., daily, weekly depending on the source).

## 2. Existing Codebase Context

### 2.1. Current Models

The system already has these database models relevant to the new features:

| Model | Description |
|-------|-------------|
| `Organization` | Companies and organizations, with fields for name, type, URL, funding round, RTO policy, accelerated vesting, and whether they give ratings |
| `ScoreType` | Types of scores (e.g., compensation, culture, benefits) |
| `ScoreAlgorithm` | Named algorithms for calculating composite scores |
| `ScoreAlgorithmWeight` | Weights linking ScoreTypes to Algorithms |
| `Score` | Individual scores given by a source organization to a target organization, with low/high threshold |

### 2.2. Key Relationships

- `Organization.gives_ratings` (boolean) identifies rating sources — the cron agent will target these.
- `Score.source` → Organization where `gives_ratings=True`
- `Score.target` → Organization being rated
- `Score.type` → ScoreType (e.g., "compensation", "culture")

## 3. Implementation Phases

### 3.1. Phase 1: Foundation

**Tasks:**
- [ ] Create the `UserPreference` model and migration
- [ ] Build the job search agent prompt and conversation flow
- [ ] Implement the agent's ability to read/write user preferences in markdown
- [ ] Set up integration with an LLM (OpenAI, Anthropic, or local model) for the conversational agent
- [ ] Define the cron job scheduling infrastructure (Celery beat, cron, or Django management command)

**Estimated effort:** 2-3 weeks

### 3.2. Phase 2: Web Search Agent — Score Gathering

**Tasks:**
- [ ] Identify and catalog all external rating sources (Glassdoor, Comparably, Blind, etc.)
- [ ] Determine which sources have APIs vs. require scraping
- [ ] Implement API integrations for sources that offer them
- [ ] Implement scraping (via Firecrawl/Playwright MCP) for sources without APIs
- [ ] Build the score extraction and normalization pipeline
- [ ] Wire up the cron job to run on a schedule
- [ ] Add logging, error handling, and alerting for failed crawls

**Estimated effort:** 3-4 weeks

### 3.3. Phase 3: Web Search Agent — Job Matching

**Tasks:**
- [ ] Identify job board sources (LinkedIn, Indeed, company career pages, etc.)
- [ ] Implement search across job sources using user preference criteria
- [ ] Cross-reference results against the `Organization` table
- [ ] Build a notification/presentation layer for matched jobs
- [ ] Wire up the cron job for periodic job searches
- [ ] Handle rate limiting and respect robots.txt / terms of service

**Estimated effort:** 2-3 weeks

### 3.4. Phase 4: Integration, Testing, and Polish

**Tasks:**
- [ ] Integration testing of both agents with the existing codebase
- [ ] User acceptance testing for the job search agent conversation flow
- [ ] Performance testing for cron jobs (ensure they complete within the window)
- [ ] Error handling, retry logic, and monitoring
- [ ] Documentation for users on how to use the job search agent
- [ ] Admin dashboard for monitoring agent runs and failures

**Estimated effort:** 2 weeks

## 4. Technology Stack Decisions

| Component | Decision | Notes |
|-----------|----------|-------|
| LLM | TBD — OpenAI API / Anthropic / local | Evaluate cost, latency, and quality |
| Web scraping | Firecrawl MCP or Playwright MCP | Flexible, both available |
| Job search | Web scraping + APIs | Prefer APIs where available |
| Scheduling | Celery beat or Django management command + cron | Simple, fits existing Django stack |
| Agent framework | LangChain or direct prompt engineering | TBD based on complexity |

## 5. Future Considerations

- Allow users to opt-in to email notifications when new matching jobs are found
- Expand to additional rating sources over time
- Build a preference editing UI (in addition to the conversational agent)
- Allow users to provide feedback on job recommendations to improve matching
- Multi-language support for international job markets

---

**Status:** Plan defined. Awaiting implementation.