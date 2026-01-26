#!/usr/bin/env python3
"""
Generate CSV template for course enrichment fields.
Reads courses.json and outputs a CSV template with columns:
name, area, difficulty, popularity_tier, drainage, exposure, winter_playability
"""

import json
import csv
import sys
from pathlib import Path

# Try to find courses.json in common locations
BASE_DIR = Path(__file__).resolve().parent.parent
COURSES_PATHS = [
    BASE_DIR / "courses.json",
    BASE_DIR / "data" / "courses.json"
]

def load_courses():
    """Load courses from JSON file."""
    for courses_path in COURSES_PATHS:
        if courses_path.exists():
            try:
                with open(courses_path, "r", encoding="utf-8") as f:
                    courses = json.load(f)
                    if isinstance(courses, list):
                        return courses
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error reading {courses_path}: {e}", file=sys.stderr)
                continue
    
    print("Error: courses.json not found", file=sys.stderr)
    sys.exit(1)

def generate_csv_template():
    """Generate CSV template from courses.json."""
    courses = load_courses()
    
    # Output CSV to stdout
    writer = csv.writer(sys.stdout)
    
    # Write header
    writer.writerow([
        "name",
        "area",
        "difficulty",
        "popularity_tier",
        "drainage",
        "exposure",
        "winter_playability"
    ])
    
    # Write course rows
    for course in courses:
        if not isinstance(course, dict):
            continue
        
        name = course.get("name", "")
        area = course.get("area", "")
        difficulty = course.get("difficulty", "")
        popularity_tier = course.get("popularity_tier", "")
        
        # Enrichment fields (empty for user to fill)
        drainage = course.get("drainage", "")
        exposure = course.get("exposure", "")
        winter_playability = course.get("winter_playability", "")
        
        writer.writerow([
            name,
            area,
            difficulty,
            popularity_tier,
            drainage,
            exposure,
            winter_playability
        ])
    
    print(f"\n# Generated {len(courses)} rows", file=sys.stderr)
    print("# Fill in drainage, exposure, winter_playability columns", file=sys.stderr)
    print("# Valid values:", file=sys.stderr)
    print("#   drainage: Poor | Average | Good", file=sys.stderr)
    print("#   exposure: Sheltered | Mixed | Exposed", file=sys.stderr)
    print("#   winter_playability: Poor | Average | Good", file=sys.stderr)

if __name__ == "__main__":
    generate_csv_template()
