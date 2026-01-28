# Alba Playability Agent — Scope Proposal

## 1. What the Playability Agent IS

### Primary Purpose

The Playability Agent is a **single-purpose web service** that answers one question: "Should I play golf at this course today or tomorrow?"

It takes a course name, optional handicap, day (Today/Tomorrow), and time of day, then returns a playability assessment with:
- A tier rating (Great, Decent, Challenging, or Rough)
- Weather and ground condition analysis
- Actionable recommendations (what to do, what to expect, alternatives)
- Plain-English explanations of why conditions are what they are

### The Core Problem It Solves

Golfers need to decide whether to play based on:
- **Weather conditions** (rain, wind, temperature)
- **Ground conditions** (how wet/soft the course is from recent rain)
- **Course characteristics** (difficulty, busyness, drainage)
- **Their own skill level** (handicap)

The Playability Agent combines all these factors into a single, clear assessment that helps golfers make informed decisions without having to:
- Check multiple weather sources
- Understand how rainfall affects course conditions
- Guess how busy a course will be
- Figure out if conditions match their skill level

### What Success Looks Like

**For users:**
- They get a clear, actionable answer within 2-3 seconds
- The assessment feels accurate and helpful (not generic or robotic)
- They understand why the tier is what it is (weather, ground, course factors)
- They know what to do (play 18, play 9, hit the range, skip it)

**For the service:**
- It works reliably even when external APIs fail (fail-open architecture)
- It renders a page for every valid request (no generic error pages for copy/LLM issues)
- It provides deterministic results (same inputs = same outputs, except for optional LLM copy)
- It handles edge cases gracefully (missing data, invalid input, API timeouts)

## 2. What the Playability Agent IS NOT

### Out of Scope

**1. User accounts or authentication**
- No login, no user profiles, no saved preferences
- No tracking of individual users across sessions
- No personalization beyond what's provided in the request (handicap, golf experience)

**2. Course booking or tee time management**
- Does not check actual tee time availability
- Does not book rounds
- Does not integrate with course booking systems
- Does not show real-time course busyness (only estimates based on day/time/weather)

**3. Course discovery or search**
- The `/courses` endpoint exists but is **supporting infrastructure**, not core
- Does not help users find new courses to play
- Does not provide course reviews, photos, or detailed course information
- Course search is only for autocomplete/typeahead in the form

**4. Feedback collection or analytics**
- The `/feedback` and `/feedback-viewer` endpoints exist but are **supporting tools**, not core
- Does not track user satisfaction or assessment accuracy
- Does not provide analytics dashboards
- Feedback collection is for internal improvement, not a user-facing feature

**5. App store redirects**
- The `/download` endpoint exists but is **marketing infrastructure**, not core
- Does not manage app downloads or installs
- Does not track conversion from web to app
- App store redirects are for funnel optimization, not playability assessment

**6. Debug or diagnostic tools**
- All `/debug/*` endpoints are **development/support tools**, not core
- Debug endpoints expose internal state for troubleshooting
- They should not be exposed to end users in production
- Debug mode (`debug=1` query param) is for internal use only

**7. Multi-day forecasts**
- Only assesses "Today" or "Tomorrow" (not 3-day, 7-day, or extended forecasts)
- Does not provide weather trends or historical comparisons
- Does not predict course conditions beyond tomorrow

**8. Course management or administration**
- Does not allow adding/editing courses through the web interface
- Course data is managed via JSON files, not through the service
- The `enrich_courses.py` script is a separate tool, not part of the service

**9. Real-time updates or notifications**
- Does not push updates when weather changes
- Does not send alerts or reminders
- Does not track changes in conditions over time

**10. Social features**
- No sharing assessments with friends
- No comparing assessments across courses
- No community ratings or reviews

## 3. Core Responsibilities (Must-Haves)

These responsibilities **must not be removed** without breaking the core value proposition.

### 3.1 Weather Data Fetching

**Must do:**
- Fetch current weather forecast for target date (Today or Tomorrow)
- Fetch historical rainfall data (last 7 days) for ground condition assessment
- Fetch tomorrow's weather if assessing "Today" (for comparison)
- Handle API failures gracefully (return `None`, use defaults, or show error page if critical)

**Must not:**
- Fail silently if weather is required and fetch fails (must show error page)
- Cache weather data indefinitely (fresh data required for accurate assessment)
- Depend on multiple weather APIs (single source of truth: Open-Meteo)

### 3.2 Playability Scoring

**Must do:**
- Calculate playability tier (Great/Decent/Challenging/Rough) using deterministic logic
- Consider weather conditions (40% weight)
- Consider ground conditions (30% weight)
- Consider course difficulty (20% weight)
- Consider busyness/pressure (10% weight)
- Adjust for handicap when provided (personalize recommendations, not tier)

**Must not:**
- Change tier calculation based on LLM output (tier is deterministic)
- Depend on external APIs for scoring (must work offline with cached weather data)
- Use non-deterministic logic for tier classification (same inputs = same tier)

### 3.3 Copy Generation (Deterministic Templates)

**Must do:**
- Generate tier-based default copy for all required fields:
  - Banner summary line
  - "Why" bullets (2-3 reasons)
  - "What you could do" bullets (2-3 actions)
  - "If not, try this instead" (1 alternative)
  - "If you do play" tips (2-3 practical tips)
- Ensure all copy fields are always populated (no empty lists, no `None` values)
- Use friendly, calm, pro-shop golfer tone (not robotic or dismissive)
- Avoid hyphens as punctuation (use periods or commas)

**Must not:**
- Fail to render a page if template generation fails (must use safe defaults)
- Generate copy that contradicts the tier (e.g., "Great" tier with "skip it" recommendations)
- Leave any required copy fields empty (always provide defaults)

### 3.4 Course Lookup

**Must do:**
- Load course data from `courses.json` file (or fallback to demo courses)
- Find course by exact name match (case-sensitive, whitespace-sensitive)
- Extract course coordinates (lat/lon) for weather fetching
- Extract course attributes (difficulty, popularity, drainage) for scoring
- Return `None` if course not found (triggers error page)

**Must not:**
- Proceed with assessment if course not found (must show error page)
- Allow fuzzy matching or partial course names (exact match required)
- Modify course data through the service (read-only)

### 3.5 HTML Rendering

**Must do:**
- Render assessment results as HTML page
- Display playability tier prominently (with color coding)
- Show all copy sections (why, what to do, alternatives, tips)
- Include debug information if `debug=1` query parameter is present
- Handle errors gracefully (show error page for hard failures, render page for soft failures)

**Must not:**
- Show generic error pages for copy/LLM failures (must render page with templates)
- Expose sensitive information in debug mode (no API keys, no internal paths)
- Break HTML rendering if any optional component fails (fail-open)

### 3.6 Input Validation

**Must do:**
- Validate required fields (course name, day, time_of_day)
- Redirect to homepage if required fields missing
- Handle optional fields gracefully (handicap can be `None`)
- Normalize input (strip whitespace, default invalid values)

**Must not:**
- Allow assessment without course name (must redirect)
- Allow assessment without day/time (must redirect)
- Crash on invalid input (must use safe defaults or redirect)

## 4. Optional Responsibilities (Nice-to-Haves)

These capabilities add value but are **not essential**. They could be simplified, isolated, or removed later without breaking the core.

### 4.1 LLM Copy Rewriting

**Current state:**
- Optional enhancement that rewrites deterministic template copy using OpenAI GPT-4o-mini
- Only runs if `OPENAI_API_KEY` environment variable is present
- Always falls back to templates if LLM fails (timeout, invalid JSON, API error)

**Why optional:**
- Service works perfectly without it (deterministic templates are sufficient)
- Adds external dependency and cost
- Adds complexity (format mapping, JSON parsing, error handling)
- Can be disabled by removing API key (fail-open architecture)

**Could be:**
- Removed entirely (service would still work)
- Moved to separate service (copy generation service)
- Simplified (remove format mapping, accept only one output format)
- Made configurable (feature flag to enable/disable)

### 4.2 Course Search API (`/courses`)

**Current state:**
- Endpoint that searches courses by name substring
- Returns up to 8 matches, sorted by relevance
- Used by frontend for autocomplete/typeahead

**Why optional:**
- Not required for core assessment flow (users can type course name)
- Could be replaced with client-side search (load all courses, filter in browser)
- Could be moved to separate service (course data service)

**Could be:**
- Removed (users type full course name)
- Simplified (return all courses, let frontend filter)
- Moved to separate service (course management service)

### 4.3 Debug Endpoints (`/debug/*`)

**Current state:**
- Multiple debug endpoints (`/debug/env`, `/debug/version`, `/debug/ui`, `/debug/checklist`, `/debug/openai`, `/debug/course`)
- Expose internal state for troubleshooting
- Used by developers/support team

**Why optional:**
- Not required for core assessment flow
- Could be replaced with logging/monitoring tools
- Could be moved to separate admin service

**Could be:**
- Removed (use logging instead)
- Gated behind authentication (admin-only)
- Moved to separate admin service

### 4.4 Feedback Collection (`/feedback`, `/feedback-viewer`)

**Current state:**
- Endpoint to submit feedback from assessment page
- Stores feedback to `data/feedback.jsonl` file
- Admin viewer to read feedback (HTTP Basic Auth)

**Why optional:**
- Not required for core assessment flow
- Could be replaced with external feedback tool (Typeform, Google Forms)
- Could be moved to separate service (feedback service)

**Could be:**
- Removed (use external feedback tool)
- Simplified (remove viewer, just store to file)
- Moved to separate service (feedback collection service)

### 4.5 App Store Redirects (`/download`)

**Current state:**
- Endpoint that detects user agent and redirects to iOS/Android app stores
- Logs download clicks with assessment context

**Why optional:**
- Not required for core assessment flow
- Could be replaced with direct links in HTML
- Could be moved to separate marketing service

**Could be:**
- Removed (use direct links)
- Simplified (remove user agent detection, show both links)
- Moved to separate service (marketing/analytics service)

### 4.6 Debug Mode (`debug=1` query parameter)

**Current state:**
- Query parameter that shows debug information in assessment results
- Exposes copy source (LLM vs templates), LLM errors, timing, payload previews

**Why optional:**
- Not required for core assessment flow
- Could be replaced with server-side logging
- Could be gated behind authentication

**Could be:**
- Removed (use logging instead)
- Gated behind authentication (admin-only)
- Simplified (remove sensitive information)

## 5. Boundaries

### What Should Live Inside This Service

**1. Assessment logic**
- Weather fetching and parsing
- Playability scoring algorithm
- Tier classification rules
- Copy template generation
- Course lookup and attribute extraction

**2. HTML rendering**
- Assessment results page
- Error pages
- Homepage form
- All UI components and styling

**3. Request handling**
- Form submission (`POST /assess`)
- Assessment results (`GET /assess`)
- Input validation and normalization
- Error handling and fallback logic

**4. Data loading**
- Course data from `courses.json` file
- Course overrides from `course_overrides.json` file
- In-memory caching of course data

### What Should Live Outside This Service

**1. Frontend (future consideration)**
- **Current state**: HTML/CSS/JS embedded in Python code
- **Future option**: Separate frontend application (React, Vue, etc.)
- **Boundary**: Service could expose JSON API, frontend handles rendering
- **Benefit**: Easier to iterate on UI without redeploying service

**2. Course data management (future consideration)**
- **Current state**: JSON files managed manually or via `enrich_courses.py` script
- **Future option**: Separate course management service or database
- **Boundary**: Service reads course data via API or database, does not write
- **Benefit**: Easier to add/edit courses without redeploying service

**3. Feedback storage (future consideration)**
- **Current state**: File-based storage (`data/feedback.jsonl`)
- **Future option**: Separate feedback service or database
- **Boundary**: Service could POST feedback to external service, not store locally
- **Benefit**: Easier to analyze feedback, no file system dependencies

**4. Analytics and monitoring (future consideration)**
- **Current state**: Basic logging to stdout
- **Future option**: External monitoring service (Datadog, Sentry, etc.)
- **Boundary**: Service logs events, monitoring service aggregates/visualizes
- **Benefit**: Better observability, alerting, performance tracking

**5. User authentication (if needed)**
- **Current state**: No authentication
- **Future option**: External auth service (Auth0, Firebase Auth, etc.)
- **Boundary**: Service validates tokens, does not manage users
- **Benefit**: No need to build auth system, focus on assessment logic

**6. LLM copy generation (future consideration)**
- **Current state**: Embedded in service, optional enhancement
- **Future option**: Separate copy generation service
- **Boundary**: Service calls copy service API, copy service handles LLM
- **Benefit**: Easier to iterate on prompts, scale independently, A/B test

### Constraints

**1. Must work standalone**
- Service must function without external dependencies (except weather API)
- Must render HTML pages (not just JSON API)
- Must handle all user interactions (form submission, results display)

**2. Must be fail-open**
- LLM failures must not break the service (fall back to templates)
- Template failures must not break the service (use safe defaults)
- Weather API failures must show error page (hard failure, cannot proceed)

**3. Must be deterministic (for scoring)**
- Playability tier must be deterministic (same inputs = same tier)
- Scoring algorithm must not depend on external APIs (except weather data)
- Copy templates must be deterministic (LLM is optional enhancement)

**4. Must be stateless**
- No session storage or user state
- Each request is independent
- No caching of assessment results (always fresh weather data)

**5. Must be simple to deploy**
- Single file (`main.py`) or minimal file structure
- No database required (file-based course data)
- No complex infrastructure (runs on Railway, Heroku, etc.)

---

**Document Version**: 1.0  
**Last Updated**: Based on codebase state as of latest commit  
**Review Status**: Scope proposal for founder review
