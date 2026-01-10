#!/usr/bin/env python3
"""
Course enrichment script that adds confidence fields to courses.json
and outputs courses_enriched.json.

This script does NOT scrape live tee times or booking pages.
Price tiers are rough estimates only.
"""

import json
import os
import sys
from typing import Dict, List, Optional


def load_courses(filepath: str) -> List[Dict]:
    """Load courses from JSON file."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            courses = json.load(f)
            if not isinstance(courses, list):
                print(f"Error: {filepath} must contain a JSON array")
                sys.exit(1)
            return courses
    except FileNotFoundError:
        print(f"Error: {filepath} not found")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {filepath}: {e}")
        sys.exit(1)


def load_overrides(filepath: str) -> Dict[str, Dict]:
    """Load manual overrides from JSON file."""
    if not os.path.exists(filepath):
        return {}
    
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            overrides = json.load(f)
            if not isinstance(overrides, dict):
                print(f"Warning: {filepath} should contain a JSON object, ignoring")
                return {}
            return overrides
    except json.JSONDecodeError as e:
        print(f"Warning: Invalid JSON in {filepath}: {e}, ignoring")
        return {}


def check_web_access() -> bool:
    """Check if web access is available."""
    try:
        import httpx
        # Try a simple request to a reliable service
        import socket
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except Exception:
        return False


def enrich_course_placeholder(course: Dict, overrides: Optional[Dict] = None) -> Dict:
    """
    Placeholder enrichment that sets all confidence to Medium and uses existing tags.
    Used when web access is unavailable or as a fallback.
    """
    enriched = course.copy()
    
    # Apply manual overrides if provided
    if overrides:
        for key, value in overrides.items():
            if key in enriched:
                enriched[key] = value
    
    # Add confidence fields (default to Medium)
    enriched["popularity_confidence"] = overrides.get("popularity_confidence", "Medium") if overrides else "Medium"
    enriched["difficulty_confidence"] = overrides.get("difficulty_confidence", "Medium") if overrides else "Medium"
    enriched["beginner_confidence"] = overrides.get("beginner_confidence", "Medium") if overrides else "Medium"
    enriched["price_confidence"] = overrides.get("price_confidence", "Medium") if overrides else "Medium"
    
    return enriched


def estimate_confidence_from_existing_tags(course: Dict) -> Dict[str, str]:
    """
    Estimate confidence levels based on existing tags.
    This is a simple heuristic - in a real implementation, this could use
    web data (without scraping booking pages) or other sources.
    """
    confidences = {}
    
    # Popularity confidence: High if tier matches common patterns
    popularity_tier = course.get("popularity_tier", "Medium")
    if popularity_tier in ["Low", "High"]:
        confidences["popularity_confidence"] = "Medium"  # Less certain for extremes
    else:
        confidences["popularity_confidence"] = "Medium"
    
    # Difficulty confidence: Medium by default
    confidences["difficulty_confidence"] = "Medium"
    
    # Beginner friendly confidence: Medium by default
    confidences["beginner_confidence"] = "Medium"
    
    # Price confidence: Low for estimates (as per requirements)
    confidences["price_confidence"] = "Low"
    
    return confidences


def enrich_course(course: Dict, overrides: Optional[Dict] = None, web_available: bool = False) -> Dict:
    """
    Enrich a single course with confidence fields.
    
    If web_available is False, uses placeholder mode.
    Otherwise, attempts to improve confidence (but still defaults to Medium).
    """
    # Apply manual overrides first
    if overrides:
        course = course.copy()
        for key, value in overrides.items():
            if key not in ["popularity_confidence", "difficulty_confidence", 
                          "beginner_confidence", "price_confidence"]:
                course[key] = value
    
    # Get confidence estimates
    if web_available:
        # In a real implementation, could use web APIs here
        # (but NOT scraping booking pages or tee times)
        confidences = estimate_confidence_from_existing_tags(course)
    else:
        # Placeholder mode: all Medium confidence
        confidences = {
            "popularity_confidence": "Medium",
            "difficulty_confidence": "Medium",
            "beginner_confidence": "Medium",
            "price_confidence": "Medium"
        }
    
    # Apply confidence overrides if provided
    if overrides:
        for conf_key in ["popularity_confidence", "difficulty_confidence", 
                        "beginner_confidence", "price_confidence"]:
            if conf_key in overrides:
                confidences[conf_key] = overrides[conf_key]
    
    # Create enriched course
    enriched = course.copy()
    enriched.update(confidences)
    
    return enriched


def enrich_courses(input_file: str = "courses.json", 
                   output_file: str = "courses_enriched.json",
                   overrides_file: str = "overrides.json") -> None:
    """
    Main enrichment function.
    
    Args:
        input_file: Path to input courses.json
        output_file: Path to output courses_enriched.json
        overrides_file: Path to manual overrides file
    """
    print(f"Loading courses from {input_file}...")
    courses = load_courses(input_file)
    print(f"Loaded {len(courses)} courses")
    
    print(f"Loading overrides from {overrides_file}...")
    all_overrides = load_overrides(overrides_file)
    print(f"Loaded {len(all_overrides)} course overrides")
    
    web_available = check_web_access()
    if not web_available:
        print("Web access unavailable, using placeholder mode (all confidence: Medium)")
    else:
        print("Web access available (using existing tags with Medium confidence)")
    
    enriched_courses = []
    for course in courses:
        course_name = course.get("name", "Unknown")
        course_overrides = all_overrides.get(course_name, {})
        
        enriched = enrich_course(course, course_overrides, web_available)
        enriched_courses.append(enriched)
        
        if course_overrides:
            print(f"  Applied overrides to: {course_name}")
    
    print(f"\nWriting enriched courses to {output_file}...")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(enriched_courses, f, indent=2, ensure_ascii=False)
    
    print(f"Successfully created {output_file} with {len(enriched_courses)} courses")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Enrich courses.json with confidence fields"
    )
    parser.add_argument(
        "--input",
        default="courses.json",
        help="Input courses file (default: courses.json)"
    )
    parser.add_argument(
        "--output",
        default="courses_enriched.json",
        help="Output enriched courses file (default: courses_enriched.json)"
    )
    parser.add_argument(
        "--overrides",
        default="overrides.json",
        help="Manual overrides file (default: overrides.json)"
    )
    
    args = parser.parse_args()
    
    enrich_courses(args.input, args.output, args.overrides)

