# Alba Playability Agent — Safe Execution Order

## Overview

This document defines a step-by-step execution order for improving the service safely. Each phase can be completed independently, production remains deployable after every phase, and risk is reduced with each step.

**Assumptions:**
- Solo founder working evenings
- Production incidents are unacceptable
- Cursor is used for most edits
- No full rewrites
- Each phase must be independently deployable

---

## Phase 1: Remove Debug Endpoints from Production

### Objective

Remove security risk from debug endpoints that expose internal state. These endpoints are development tools and should not be accessible in production.

### Scope of Change

**What is allowed to change:**
- Remove all `/debug/*` endpoints (`/debug/env`, `/debug/version`, `/debug/ui`, `/debug/checklist`, `/debug/openai`, `/debug/course`)
- Remove `debug=1` query parameter handling from `/assess` endpoint
- Remove debug information rendering from assessment results page
- Keep debug logging to stdout (for Railway logs)

**What must not change:**
- Core assessment logic (`render_assessment_results`, `compute_playability`, etc.)
- Error handling behavior (still log errors, still show error pages)
- LLM integration (still works, just no debug output)
- Course search endpoint (`/courses` - keep this, it's used by frontend)

### Success Criteria

1. All `/debug/*` endpoints return 404 in production
2. `debug=1` query parameter has no effect (ignored silently)
3. Assessment results page renders normally without debug sections
4. Error logging still works (check Railway logs after deploy)
5. Manual test: Run 5 assessments, confirm all work without debug output

### Failure Guardrails

**Before moving to Phase 2:**
- Deploy to production and verify no debug endpoints are accessible
- Run 10 manual assessments to confirm normal operation
- Check Railway logs to confirm error logging still works
- Wait 24 hours to monitor for any production issues
- If any issues occur, rollback immediately and fix before proceeding

**Rollback plan:**
- Git revert the commit that removed debug endpoints
- Redeploy immediately
- Debug endpoints will be restored

---

## Phase 2: Add Basic Rate Limiting

### Objective

Protect the service from abuse and accidental DoS attacks by limiting request frequency per IP address.

### Scope of Change

**What is allowed to change:**
- Add rate limiting middleware to FastAPI app
- Limit `/assess` endpoint to 10 requests per minute per IP
- Limit `/courses` endpoint to 20 requests per minute per IP
- Return HTTP 429 (Too Many Requests) when limit exceeded
- Log rate limit violations (IP address, endpoint, timestamp)

**What must not change:**
- Core assessment logic (no changes to scoring, weather fetching, etc.)
- Error handling behavior (rate limit is new error type, handled like other errors)
- Response format (429 response is plain text, not HTML)
- Other endpoints (homepage `/` is not rate limited)

### Success Criteria

1. Rate limiting middleware is active on `/assess` and `/courses`
2. Normal users can make 10 assessments per minute without issues
3. Users exceeding limit see HTTP 429 error (not crash)
4. Rate limit violations are logged with IP address
5. Manual test: Make 11 requests rapidly, confirm 11th returns 429

### Failure Guardrails

**Before moving to Phase 3:**
- Deploy to production and monitor for false positives (legitimate users blocked)
- Check Railway logs for rate limit violations
- Wait 48 hours to observe normal traffic patterns
- If legitimate users are blocked, increase limit or adjust algorithm
- If service crashes on rate limit, rollback immediately

**Rollback plan:**
- Remove rate limiting middleware code
- Redeploy immediately
- Service returns to unlimited requests

---

## Phase 3: Improve Course Name Matching

### Objective

Reduce user frustration from "course not found" errors caused by exact string matching. Make course lookup more forgiving while maintaining accuracy.

### Scope of Change

**What is allowed to change:**
- Modify `find_course_by_name()` function to use case-insensitive matching
- Normalize course names (trim whitespace, collapse multiple spaces)
- Add fuzzy matching fallback (if exact match fails, try case-insensitive)
- Keep exact match as primary (fastest, most accurate)
- Log when fuzzy match is used (for monitoring)

**What must not change:**
- Course data structure (`courses.json` format unchanged)
- Assessment logic (only course lookup changes, not scoring)
- Error handling (still returns `None` if no match found, still shows error page)
- Course search endpoint (`/courses` - keep existing behavior)

### Success Criteria

1. "Royal Blackheath" matches "royal blackheath" (case-insensitive)
2. "Royal  Blackheath" matches "Royal Blackheath" (whitespace normalized)
3. Exact matches still work (no performance regression)
4. Fuzzy matches are logged (check Railway logs)
5. Manual test: Try 5 variations of course names, confirm all match correctly

### Failure Guardrails

**Before moving to Phase 4:**
- Deploy to production and monitor for incorrect matches
- Check Railway logs for fuzzy match usage
- Wait 24 hours to observe user behavior
- If incorrect matches occur (wrong course selected), rollback immediately
- If performance degrades, optimize matching algorithm

**Rollback plan:**
- Revert to exact string matching (`course["name"] == course_name`)
- Redeploy immediately
- Course lookup returns to original behavior

---

## Phase 4: Simplify LLM Format Mapping

### Objective

Reduce complexity in LLM response handling by accepting only one output format (Format A) and removing legacy format support (Format B).

### Scope of Change

**What is allowed to change:**
- Update `llm_rewrite_assessment_copy()` to accept only Format A keys
- Remove Format B key mapping logic (`headline` → `banner_summary_line`, etc.)
- Update LLM prompt to explicitly require Format A only
- Simplify `post_process_llm_output()` to remove format conversion
- Keep lenient validation (still accept missing optional keys, still fill defaults)

**What must not change:**
- LLM fail-open behavior (still returns `None` on error, still falls back to templates)
- Template fallback (still works if LLM fails)
- Required keys validation (still rejects truly unusable output)
- LLM timeout and retry logic (unchanged)

### Success Criteria

1. LLM prompt explicitly requires Format A keys only
2. Format B key mapping code is removed
3. LLM output validation accepts only Format A
4. Template fallback still works if LLM fails
5. Manual test: Run 5 assessments with LLM enabled, confirm all render correctly

### Failure Guardrails

**Before moving to Phase 5:**
- Deploy to production and monitor LLM success rate
- Check Railway logs for LLM errors (should not increase)
- Wait 48 hours to observe LLM behavior
- If LLM success rate drops significantly, rollback immediately
- If template fallback breaks, rollback immediately

**Rollback plan:**
- Restore Format B key mapping logic
- Update LLM prompt to accept both formats
- Redeploy immediately
- LLM returns to dual-format support

---

## Phase 5: Improve Error Messages

### Objective

Help users understand what went wrong without exposing internal details. Replace generic error messages with specific, actionable guidance.

### Scope of Change

**What is allowed to change:**
- Update error page message to be more specific
- Add different error messages for different failure types:
  - "Course not found" → "We couldn't find that course. Check the spelling and try again."
  - "Weather API failed" → "We couldn't get weather data right now. Please try again in a moment."
  - "Invalid input" → "Something went wrong with your request. Please go back and try again."
- Keep generic fallback message for unexpected errors
- Log detailed error information server-side (unchanged)

**What must not change:**
- Error page rendering logic (still shows error page for hard failures)
- Error logging (still logs full details with `request_id`)
- Fail-open behavior (soft failures still render page, not error page)
- Error detection logic (still distinguishes hard vs soft failures)

### Success Criteria

1. Error page shows specific message based on error type
2. Generic fallback message exists for unexpected errors
3. Error logging still includes full details (check Railway logs)
4. Users can understand what went wrong
5. Manual test: Trigger each error type, confirm correct message displays

### Failure Guardrails

**Before moving to Phase 6:**
- Deploy to production and monitor error page usage
- Check Railway logs to confirm error types are correctly identified
- Wait 24 hours to observe user behavior
- If error messages are confusing or incorrect, fix immediately
- If error detection breaks (wrong error type shown), rollback immediately

**Rollback plan:**
- Restore generic error message ("We couldn't check playability...")
- Redeploy immediately
- Error page returns to original behavior

---

## Phase 6: Add Structured Logging

### Objective

Improve observability without adding complexity. Make it easier to debug production issues by standardizing log format and adding request context.

### Scope of Change

**What is allowed to change:**
- Add structured logging format (JSON) for key events
- Add request context to all log messages (`request_id`, `course`, `handicap`, etc.)
- Standardize log levels (INFO for normal flow, WARNING for fallbacks, ERROR for failures)
- Keep existing log messages (just reformat, don't remove)
- Add log message for assessment start and completion

**What must not change:**
- Log output destination (still stdout, still captured by Railway)
- Error logging behavior (still logs full tracebacks on errors)
- LLM logging (still logs LLM calls, timeouts, errors)
- Assessment logic (no changes to core functionality)

### Success Criteria

1. All log messages include `request_id` when available
2. Log messages are structured (consistent format, easy to parse)
3. Key events are logged (assessment start, completion, errors)
4. Railway logs are still readable (not just JSON blobs)
5. Manual test: Run 5 assessments, check Railway logs for structured output

### Failure Guardrails

**Before considering this phase complete:**
- Deploy to production and verify logs are readable in Railway
- Check that `request_id` appears in all relevant log messages
- Wait 24 hours to observe log volume and format
- If logs become unreadable or too verbose, adjust format
- If log volume increases significantly, optimize logging frequency

**Rollback plan:**
- Revert to original logging format (unstructured strings)
- Redeploy immediately
- Logging returns to original behavior

---

## General Principles

### Deployment Safety

1. **One phase at a time**: Complete Phase 1 fully before starting Phase 2
2. **Test in production**: Each phase must be deployed and monitored before moving on
3. **Rollback ready**: Every phase has a clear rollback plan
4. **Monitor closely**: Watch Railway logs and user behavior for 24-48 hours after each deploy

### Risk Management

1. **No breaking changes**: Each phase must not break existing functionality
2. **Fail-open preserved**: LLM failures, template failures still render pages
3. **Error handling intact**: Hard failures still show error pages, soft failures still render
4. **Core logic untouched**: Assessment scoring, weather fetching, course lookup unchanged

### Success Measurement

1. **Production stability**: No incidents or crashes
2. **User experience**: Users can still complete assessments successfully
3. **Observability**: Easier to debug issues when they occur
4. **Risk reduction**: Each phase reduces a specific risk identified in production notes

---

## Phase Order Rationale

**Phase 1 (Remove Debug Endpoints)** - Highest security risk, easiest to remove, zero impact on core functionality

**Phase 2 (Rate Limiting)** - Prevents abuse, low complexity, clear failure mode (429 error)

**Phase 3 (Course Name Matching)** - High user impact (reduces frustration), low technical risk, easy to test

**Phase 4 (Simplify LLM)** - Reduces complexity, medium risk (LLM integration), but fail-open protects us

**Phase 5 (Error Messages)** - Improves user experience, low technical risk, easy to test

**Phase 6 (Structured Logging)** - Improves observability, low risk (logging only), helps with future debugging

Each phase builds on the previous ones, but can be completed independently. If any phase causes issues, rollback and fix before proceeding.

---

**Document Version**: 1.0  
**Last Updated**: Based on production notes and scope proposal  
**Review Status**: Execution order for safe improvement
