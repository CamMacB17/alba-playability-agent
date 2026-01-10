# Logic Examples

This document provides example scenarios demonstrating the daylight feasibility logic for recommended holes.

## Winter Scenario: 9 Holes Recommended

**Input:**
- Course: Trent Park
- Handicap: 20
- Day: Today
- Time of day: Afternoon (14:30)
- Month: December
- Busyness rating: Moderate

**Calculations:**
- Tee time: 14:30
- Sunset time (December fallback): 16:45
- Daylight minutes: 135 minutes (2 hours 15 minutes)
- Expected duration (Moderate): 18 holes = 270 mins, 9 holes = 135 mins

**Result:**
- Recommended holes: 9 (daylight_minutes (135) >= duration_9 (135), but < duration_18 (270))
- Daylight label: "Tight" (135 - 135 = 0 minutes margin, < 30)
- Verdict: Based on other factors (daylight is feasible for 9 holes)

**Summary excerpt:** "9 holes is the safer call for daylight."

## Summer Scenario: 18 Holes Feasible at Afternoon

**Input:**
- Course: Red Libbets
- Handicap: 15
- Day: Tomorrow
- Time of day: Afternoon (14:30)
- Month: June
- Busyness rating: Quiet

**Calculations:**
- Tee time: 14:30
- Sunset time (June fallback): 21:15
- Daylight minutes: 405 minutes (6 hours 45 minutes)
- Expected duration (Quiet): 18 holes = 240 mins, 9 holes = 120 mins

**Result:**
- Recommended holes: 18 (daylight_minutes (405) >= duration_18 (240))
- Daylight label: "Plenty of light" (405 - 240 = 165 minutes margin, >= 30)
- Verdict: Based on other factors

**Summary excerpt:** "18 holes looks feasible before sunset."

## Additional Scenarios

### Very Busy Course in Winter Evening

**Input:**
- Time of day: Evening (16:30)
- Month: January
- Busyness rating: Very busy

**Calculations:**
- Tee time: 16:30
- Sunset time (January fallback): 16:30
- Daylight minutes: 0 minutes
- Expected duration (Very busy): 18 holes = 300 mins, 9 holes = 150 mins

**Result:**
- Recommended holes: 9
- Daylight label: "Not feasible" (0 < duration_9 (150))
- Verdict: "Don't play" (daylight label = "Not feasible" forces this)

### Moderate Busyness in Spring Morning

**Input:**
- Time of day: Morning (09:00)
- Month: April
- Busyness rating: Moderate

**Calculations:**
- Tee time: 09:00
- Sunset time (April fallback): 19:45
- Daylight minutes: 645 minutes (10 hours 45 minutes)
- Expected duration (Moderate): 18 holes = 270 mins, 9 holes = 135 mins

**Result:**
- Recommended holes: 18 (daylight_minutes (645) >= duration_18 (270))
- Daylight label: "Plenty of light" (645 - 270 = 375 minutes margin, >= 30)
- Verdict: Based on other factors

**Summary excerpt:** "18 holes looks feasible before sunset."

