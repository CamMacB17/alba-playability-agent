# Course Enrichment Guide

## Overview

This guide explains how to manually fill in enrichment fields for courses: `drainage`, `exposure`, and `winter_playability`.

## Field Definitions

### drainage
**Values:** `Poor` | `Average` | `Good`

How well the course drains after rainfall:
- **Poor**: Course holds water, stays soft longer after rain (e.g., low-lying, clay soil)
- **Average**: Standard drainage, typical recovery time
- **Good**: Excellent drainage, recovers quickly (e.g., sandy soil, well-drained)

### exposure
**Values:** `Sheltered` | `Mixed` | `Exposed`

How exposed the course is to wind:
- **Sheltered**: Protected from wind (e.g., tree-lined, valley courses)
- **Mixed**: Some protection, some exposure
- **Exposed**: Very exposed to wind (e.g., coastal, hilltop courses)

### winter_playability
**Values:** `Poor` | `Average` | `Good`

How playable the course is in cold + wet conditions:
- **Poor**: Struggles in winter (e.g., heavy ground, poor drainage)
- **Average**: Standard winter playability
- **Good**: Holds up well in winter (e.g., good drainage, firm base)

## How to Fill Values

1. **Generate CSV template**: `python scripts/course_enrich_template.py > course_enrichment.csv`
2. **Fill in values** for each course based on your knowledge
3. **Import back** to courses.json (manual process for now)

## Defaults

If fields are missing, defaults are: `drainage=Average`, `exposure=Mixed`, `winter_playability=Average`
