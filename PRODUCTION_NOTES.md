# Alba Playability Agent — Production Notes (Current State)

## 1. What This Service Does

### Core User Flow

1. **User submits form** (`POST /assess`) with:
   - Course name (required)
   - Handicap (optional)
   - Golf experience level: Beginner/Regular/Confident (default: Regular)
   - Day: Today/Tomorrow (required)
   - Time of day: Morning/Afternoon/Evening (required)

2. **Request redirects to GET endpoint** (`GET /assess`) with query parameters

3. **Assessment pipeline executes** (`render_assessment_results`):
   - **Course lookup**: Finds course by name from `courses.json` (returns `None` if not found)
   - **Date calculation**: Parses "Today" or "Tomorrow" into ISO date string
   - **Weather fetch**: Calls Open-Meteo API for:
     - Target date forecast (temperature, wind, precipitation, sunset)
     - Historical rainfall (last 7 days) for ground conditions
     - Tomorrow's weather (if today) for comparison
   - **Deterministic scoring**: Calculates playability tier (Great/Decent/Challenging/Rough) using:
     - Weather conditions (40% weight)
     - Ground conditions (30% weight)
     - Course difficulty (20% weight)
     - Busyness/pressure (10% weight)
   - **Copy generation**: Two-stage process:
     - **Stage 1 (Deterministic)**: Generates template copy with tier-based defaults
     - **Stage 2 (LLM-assisted, optional)**: Rewrites template copy using OpenAI GPT-4o-mini if `OPENAI_API_KEY` is present

4. **Response rendered**: HTML page with assessment results, recommendations, and debug info (if `debug=1`)

### Deterministic vs LLM-Assisted

**Deterministic (always runs):**
- Playability tier classification (Great/Decent/Challenging/Rough)
- Weather label calculation (Dry/Showers/Rain/Windy/Cold/Very cold)
- Ground condition assessment (Firm/Normal/Soft/Too soft)
- Busyness rating (Quiet/Moderate/Busy/Very busy)
- Base recommendations (action + reason pairs)
- Template copy generation (tier-based defaults)

**LLM-Assisted (optional, fail-open):**
- Copy rewriting for natural language tone
- Banner summary line generation
- Bullet point refinement
- Only runs if `OPENAI_API_KEY` environment variable is present
- Always falls back to templates if LLM fails (timeout, invalid JSON, API error, missing keys)

## 2. Critical Production Dependencies

### External APIs

**Open-Meteo API** (no API key required)
- Endpoint: `https://api.open-meteo.com/v1/forecast`
- Timeout: 10 seconds per request
- Used for:
  - Daily forecast (temperature, wind, precipitation, sunset)
  - Historical rainfall (7-day sum)
- Failure behavior: Returns `None` on any error (network, timeout, invalid response)
- Impact: If weather fetch fails completely, `render_assessment_results` raises `ValueError("Weather data fetch failed")` → shows error page

### Environment Variables

**Required:**
- None (service runs without any env vars)

**Optional:**
- `OPENAI_API_KEY`: Enables LLM copy rewriting (default-on if present)
- `RAILWAY_GIT_COMMIT_SHA`: Used for debug/version info
- `GITHUB_SHA`: Fallback for version info
- `RAILWAY_SERVICE_NAME`: Used for debug info
- `ALBA_IOS_URL`: iOS app store link (default: Apple App Store URL)
- `ALBA_ANDROID_URL`: Android app store link (default: Google Play URL)
- `ALBA_HOW_IT_WORKS_URL`: Help page URL (default: blog URL)
- `FEEDBACK_USER`: Basic auth username for feedback viewer
- `FEEDBACK_PASS`: Basic auth password for feedback viewer

### Data Files

**`courses.json`** (required at runtime)
- Location: `BASE_DIR/courses.json` or `BASE_DIR/data/courses.json` (fallback)
- Format: JSON array of course objects
- Required fields per course: `name` (string), `lat` (float), `lon` (float)
- Optional fields: `popularity_tier`, `difficulty`, `beginner_friendly`, `price_tier`, `drainage`, `exposure`, `winter_playability`
- Failure behavior: If file missing or invalid JSON, falls back to `DEMO_COURSES` (hardcoded list)
- Course lookup: `find_course_by_name()` returns `None` if course not found → triggers error page

**`course_overrides.json`** (optional)
- Location: `BASE_DIR/course_overrides.json`
- Format: JSON object mapping course names to override fields
- Used to manually correct course attributes without editing main file

### Runtime Assumptions

1. **Python 3.12+** (uses `asyncio.to_thread` for OpenAI client)
2. **FastAPI + Uvicorn** (async web framework)
3. **Network access** to Open-Meteo API (no VPN/proxy required)
4. **System timezone** matches user's timezone (for date calculations)
5. **File system access** to read `courses.json` at startup and per-request (cached in memory)

## 3. Failure Behavior

### Weather API Failure

**What happens:**
- `fetch_weather_data()` catches all exceptions and returns `None`
- `fetch_historical_rainfall()` catches all exceptions and returns `None`
- `render_assessment_results()` checks if `weather_data is None` after fetch attempt
- If `course_data` exists but weather fetch fails, raises `ValueError("Weather data fetch failed")`

**User sees:**
- Error page: "We couldn't check playability for that selection. Try a different course or time."
- Error logged with full traceback: `logger.error(f"Error fetching weather data (request_id={request_id}): {str(e)}", exc_info=True)`

**Fail-open behavior:**
- If weather data is `None` but no exception raised, `calculate_weather_label()` returns defaults:
  - `weather_label: "Dry"`
  - `weather_rating: "Dry"`
- Assessment continues with default weather values

### LLM Failure

**Failure modes:**
1. **API key missing**: `OPENAI_API_KEY` not set → LLM disabled, uses templates
2. **OpenAI package missing**: Import error → LLM disabled, uses templates
3. **API timeout**: 20-second timeout exceeded → Returns `None`, falls back to templates
4. **Invalid JSON response**: JSON parse fails → Returns `None`, falls back to templates
5. **Missing critical keys**: LLM output missing required fields → Returns `None`, falls back to templates
6. **API error**: Rate limit, authentication error, etc. → Returns `None`, falls back to templates
7. **Unexpected exception**: Any other error in `llm_rewrite_assessment_copy()` → Returns `None`, falls back to templates

**What happens:**
- `llm_rewrite_assessment_copy()` **never raises exceptions** — always returns `None` on error
- `build_final_copy()` checks `if llm_output is None` → falls back to templates
- `render_assessment_results()` wraps copy generation in `try/except` → falls back to templates on any error

**User sees:**
- Page renders normally with template copy (no error banner)
- Debug info (if `debug=1`) shows `copy_source: "templates"` and `llm_error_type`/`llm_error_message`

**Fail-open behavior:**
- LLM failures are **never** shown to users
- Page always renders with deterministic template copy
- Only hard failures (course not found, weather API failure) show error page

### Course Data Missing

**What happens:**
- `find_course_by_name()` returns `None` if course not found
- `render_assessment_results()` checks `if course_data:` before weather fetch
- If `course_data is None`, raises `ValueError("Course not found")` (hard failure)

**User sees:**
- Error page: "We couldn't check playability for that selection. Try a different course or time."
- Error logged: `logger.error(f"Hard failure rendering assessment results (request_id={request_id}): {str(e)}", exc_info=True)`

**Fail-open behavior:**
- None — course lookup failure is a hard failure (cannot proceed without course coordinates)

### Invalid Input (Date/Time)

**What happens:**
- Date parsing wrapped in `try/except` in `render_assessment_results()`
- On exception, logs error and uses defaults (`today`, current `day_of_week`, current `month`)
- Assessment continues with default date values

**User sees:**
- Page renders normally (no error)
- Assessment uses today's date instead of requested date

**Fail-open behavior:**
- Invalid date/time input does not crash — uses safe defaults

### Template Fallback Failure

**What happens:**
- `build_final_copy()` wrapped in `try/except` in `render_assessment_results()`
- If template generation fails, uses absolute minimum safe defaults:
  - `playability_tier: "Decent"`
  - `best_move: "18 holes"`
  - `why_bullets: ["Conditions are suitable for golf today."]`
  - `what_you_could_do_bullets: ["18 holes works well today. Conditions are manageable."]`
  - `instead_activities: ["9 holes or a range session gives you good value if you're pressed for time."]`
  - `if_you_play_tips: ["Keep an eye on the weather. Conditions can change, so be prepared."]`

**User sees:**
- Page renders normally with safe default copy
- No error banner

**Fail-open behavior:**
- Multiple layers of fallbacks ensure page always renders

## 4. Safety Mechanisms Already in Place

### Fail-Open Architecture

**LLM copy generation:**
- `llm_rewrite_assessment_copy()` never raises — always returns `None` on error
- `build_final_copy()` checks `if llm_output is None` → falls back to templates
- `render_assessment_results()` wraps copy generation in `try/except` → falls back to templates

**Template copy generation:**
- `ensure_assessment_defaults()` fills missing keys with tier-based defaults
- `render_assessment_results()` wraps template building in `try/except` → uses safe defaults on error
- Multiple fallback layers: deterministic → templates → safe defaults → absolute minimums

**Weather API:**
- Returns `None` on error (does not raise)
- `calculate_weather_label()` handles `None` input → returns defaults
- Only raises if `course_data` exists but weather fetch explicitly fails (hard failure)

### Template Fallbacks

**Tier-based defaults** (`ensure_assessment_defaults()`):
- `what_you_could_do_bullets`: 2 bullets for Great/Decent, 3 for Challenging/Rough
- `instead_activities`: Always 1 bullet, tier-based content
- `if_you_play_tips`: Always 2-3 bullets, tier-based content
- `if_not_try_this_instead`: Derived from `instead_activities`, always populated

**Safe defaults** (if template building fails):
- Absolute minimum copy that always renders
- No empty lists, no `None` values, all strings non-empty

### Error Handling Paths

**Hard failures** (show error page):
- Course not found (`course_data is None`)
- Weather API failure (if `course_data` exists but fetch fails)
- Invalid date/time input (if cannot parse at all)

**Soft failures** (fail-open, render page):
- LLM timeout/invalid JSON/missing keys → templates
- Template building failure → safe defaults
- Copy generation exception → safe defaults
- Weather data `None` (but no exception) → default weather values

**Logging:**
- All errors logged with `request_id` for traceability
- Hard failures logged with `exc_info=True` (full traceback)
- LLM errors logged with duration, timeout, model, parse stage
- Copy generation errors logged with error type and message

### Input Validation

**Course name:**
- Stripped of whitespace
- Redirects to `/` if empty or missing

**Date/time:**
- Validates `day` and `time_of_day` are present
- Redirects to `/` if missing
- Parses "Today"/"Tomorrow" with fallback to today

**Handicap:**
- Optional (can be `None`)
- No validation if provided (assumed integer)

**Golf experience:**
- Defaults to "Regular" if invalid value
- Valid values: "Beginner", "Regular", "Confident"

## 5. Known Production Risks

### High Complexity Areas

**1. Copy generation pipeline** (`build_final_copy()` → `llm_rewrite_assessment_copy()` → `post_process_llm_output()`)
- **Risk**: Multiple format mappings (Format A vs Format B), key transformations, type conversions
- **Mitigation**: Lenient validation accepts both formats, maps legacy keys, converts types
- **Failure mode**: If mapping fails, falls back to templates

**2. LLM response parsing** (`parse_llm_payload()` + JSON salvage)
- **Risk**: LLM may return markdown-wrapped JSON, invalid JSON, or missing keys
- **Mitigation**: Multiple salvage attempts (extract JSON from markdown, parse as plaintext)
- **Failure mode**: If all salvage fails, returns `None` → templates

**3. Playability scoring** (`compute_playability()`)
- **Risk**: Complex weighted scoring with multiple factors, tier classification rules, handicap adjustments
- **Mitigation**: Deterministic logic, no external dependencies
- **Failure mode**: If scoring fails, tier defaults to "Decent"

### Single Points of Failure

**1. Open-Meteo API**
- **Risk**: Single external API for all weather data
- **Impact**: If API is down, all assessments fail (hard failure)
- **Mitigation**: 10-second timeout, returns `None` on error
- **No redundancy**: No fallback weather API

**2. `courses.json` file**
- **Risk**: Single source of truth for course data
- **Impact**: If file missing/invalid, falls back to `DEMO_COURSES` (hardcoded list)
- **Mitigation**: File validation, fallback to demo courses, course lookup returns `None` if not found
- **No redundancy**: No database backup or external course API

**3. Course name matching**
- **Risk**: Exact string match (`course["name"] == course_name`)
- **Impact**: Case-sensitive, whitespace-sensitive — "Royal Blackheath" ≠ "royal blackheath"
- **Mitigation**: None — requires exact match
- **User impact**: Typos or case mismatches → course not found → error page

### Fragile or Tightly Coupled Areas

**1. LLM output format expectations**
- **Risk**: Code expects specific JSON keys (`banner_summary_line`, `what_you_could_do_bullets`, etc.)
- **Mitigation**: Lenient validation accepts Format A and Format B, maps legacy keys
- **Fragility**: If LLM changes output format significantly, mapping may fail → templates

**2. Template copy structure**
- **Risk**: Template generation assumes specific key names and types
- **Mitigation**: `ensure_assessment_defaults()` fills missing keys, validates types
- **Fragility**: If template structure changes, defaults may not match → safe defaults

**3. Date/time parsing**
- **Risk**: Assumes "Today"/"Tomorrow" strings, system timezone matches user timezone
- **Mitigation**: Wrapped in `try/except`, uses defaults on error
- **Fragility**: If date format changes, parsing may fail → uses today's date

**4. Weather data structure**
- **Risk**: Assumes Open-Meteo API response structure (`daily.time`, `daily.temperature_2m_max`, etc.)
- **Mitigation**: Checks for key existence before access, returns `None` on error
- **Fragility**: If API response format changes, parsing may fail → returns `None` → default weather

**5. Course data structure**
- **Risk**: Assumes `courses.json` has specific fields (`name`, `lat`, `lon`, `popularity_tier`, etc.)
- **Mitigation**: Validates required fields, normalizes optional fields with defaults
- **Fragility**: If course structure changes, validation may reject valid courses → falls back to demo courses

### Other Production Considerations

**1. No rate limiting**
- **Risk**: No per-IP or per-user rate limiting on `/assess` endpoint
- **Impact**: Potential for abuse or accidental DoS

**2. No caching**
- **Risk**: Weather API called on every request (even for same course/date)
- **Impact**: Unnecessary API calls, slower responses
- **Mitigation**: Open-Meteo is free and fast, but could add caching layer

**3. Synchronous course loading**
- **Risk**: `load_courses()` called on every request (though results cached in memory)
- **Impact**: File I/O on every request if cache misses
- **Mitigation**: Results cached, but cache not explicitly managed

**4. Debug mode exposure**
- **Risk**: `debug=1` query parameter exposes internal state (LLM errors, payloads, timing)
- **Impact**: Information leakage, potential security risk
- **Mitigation**: Only shows debug info, no sensitive data exposed

**5. Error page message**
- **Risk**: Generic error message ("We couldn't check playability...") doesn't help users debug
- **Impact**: Users don't know if course name was wrong, weather API failed, etc.
- **Mitigation**: Errors logged server-side with `request_id` for support

---

**Document Version**: 1.0  
**Last Updated**: Based on codebase state as of latest commit  
**Review Status**: Current production state documented
