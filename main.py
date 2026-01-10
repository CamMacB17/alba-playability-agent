import json
import os
import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from urllib.parse import urlencode
import httpx
from typing import Dict, Any, Tuple, List
from uuid import uuid4

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Base directory and paths
BASE_DIR = Path(__file__).resolve().parent
COURSES_PATH = BASE_DIR / "courses.json"
COURSES_PATH_FALLBACK = BASE_DIR / "data" / "courses.json"

app = FastAPI()

# Get git commit hash at startup
def get_git_commit() -> str:
    """Get the latest git commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=os.path.dirname(__file__)
        )
        if result.returncode == 0:
            return result.stdout.strip()[:7]  # Return short hash
    except Exception:
        pass
    return "unknown"

# Generate build time at startup
BUILD_TIME_UTC = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
GIT_COMMIT = get_git_commit()

# Feature flag and API key for OpenAI
LLM_SUMMARY_ENABLED = os.getenv("LLM_SUMMARY", "false").lower() == "true"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Initialize OpenAI client if API key is available
openai_client = None
if OPENAI_API_KEY and LLM_SUMMARY_ENABLED:
    try:
        from openai import AsyncOpenAI
        openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=10.0)
    except ImportError:
        openai_client = None

# Fallback demo courses if courses.json is missing or invalid
DEMO_COURSES = [
    {"name": "Demo Course 1", "lat": 51.5074, "lon": -0.1278, "popularity_tier": "Low", "difficulty": "Easy", "beginner_friendly": "Yes", "price_tier": "£"},
    {"name": "Demo Course 2", "lat": 51.5155, "lon": -0.0922, "popularity_tier": "Medium", "difficulty": "Medium", "beginner_friendly": "Mixed", "price_tier": "££"}
]


def load_courses():
    """Load courses from courses.json, fall back to demo courses if file is missing or invalid."""
    courses_file = os.path.join(os.path.dirname(__file__), "courses.json")
    
    try:
        if os.path.exists(courses_file):
            with open(courses_file, "r", encoding="utf-8") as f:
                courses = json.load(f)
                # Validate that courses is a list
                if isinstance(courses, list):
                    # Validate and normalize each course
                    valid_courses = []
                    for course in courses:
                        if not isinstance(course, dict):
                            continue
                        
                        # Check required fields: name (non-empty string), lat and lon (numbers or convertible to floats)
                        name = course.get("name")
                        if not name or not isinstance(name, str) or not name.strip():
                            continue
                        
                        lat = course.get("lat")
                        lon = course.get("lon")
                        
                        # Try to convert lat/lon to float if they're strings
                        try:
                            lat_float = float(lat) if lat is not None else None
                            lon_float = float(lon) if lon is not None else None
                        except (ValueError, TypeError):
                            lat_float = None
                            lon_float = None
                        
                        # Both lat and lon must be present and valid numbers
                        if lat_float is None or lon_float is None:
                            continue
                        
                        # Normalize course: ensure area field exists (default to empty string)
                        normalized_course = dict(course)
                        if "area" not in normalized_course:
                            normalized_course["area"] = ""
                        
                        valid_courses.append(normalized_course)
                    
                    if valid_courses:
                        return valid_courses
    except (json.JSONDecodeError, IOError, OSError):
        pass
    
    # Fall back to demo courses
    return DEMO_COURSES


def find_course_by_name(course_name: str):
    """Find a course by name from the loaded courses."""
    courses = load_courses()
    for course in courses:
        if course["name"] == course_name:
            return course
    return None


def load_courses_from_data(debug_info: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """
    Load courses from courses.json using absolute path.
    Tries BASE_DIR/courses.json first, then falls back to BASE_DIR/data/courses.json.
    Returns list of course dicts with name, lat, lon (required) and area (optional, defaults to empty string).
    
    If debug_info dict is provided, populates debug fields for the file that was attempted.
    """
    # Try primary path first (root courses.json)
    for courses_path in [COURSES_PATH, COURSES_PATH_FALLBACK]:
        try:
            if courses_path.exists():
                # Collect debug info if requested
                if debug_info is not None:
                    try:
                        file_size = courses_path.stat().st_size
                        debug_info["file_size_bytes"] = file_size
                        
                        with open(courses_path, "r", encoding="utf-8") as f:
                            file_content = f.read()
                            debug_info["file_head"] = file_content[:200]
                            f.seek(0)
                            courses = json.load(f)
                    except Exception as e:
                        debug_info["parse_error"] = str(e)
                        debug_info["detected_format"] = "unknown"
                        logger.error(f"Failed to load courses from {courses_path}: {str(e)}", exc_info=True)
                        continue
                else:
                    with open(courses_path, "r", encoding="utf-8") as f:
                        courses = json.load(f)
                
                # Validate that courses is a list
                if isinstance(courses, list):
                    # Detect format
                    if debug_info is not None:
                        debug_info["parse_error"] = ""
                        if len(courses) > 0:
                            first_item = courses[0]
                            if isinstance(first_item, dict):
                                # Check if it has name and (lat/lon or area)
                                has_name = "name" in first_item
                                has_coords = "lat" in first_item and "lon" in first_item
                                has_area = "area" in first_item
                                if has_name and (has_coords or has_area):
                                    debug_info["detected_format"] = "list_of_objects"
                                else:
                                    debug_info["detected_format"] = "unknown"
                            elif isinstance(first_item, str):
                                debug_info["detected_format"] = "list_of_strings"
                            else:
                                debug_info["detected_format"] = "unknown"
                        else:
                            debug_info["detected_format"] = "unknown"
                    
                    # Validate and normalize courses
                    valid_courses = []
                    for course in courses:
                        if not isinstance(course, dict):
                            continue
                        
                        # Check required fields: name (non-empty string), lat and lon (numbers or convertible to floats)
                        name = course.get("name")
                        if not name or not isinstance(name, str) or not name.strip():
                            continue
                        
                        lat = course.get("lat")
                        lon = course.get("lon")
                        
                        # Try to convert lat/lon to float if they're strings
                        try:
                            lat_float = float(lat) if lat is not None else None
                            lon_float = float(lon) if lon is not None else None
                        except (ValueError, TypeError):
                            lat_float = None
                            lon_float = None
                        
                        # Both lat and lon must be present and valid numbers
                        if lat_float is None or lon_float is None:
                            continue
                        
                        # Normalize course: ensure area field exists (default to empty string)
                        normalized_course = dict(course)
                        if "area" not in normalized_course:
                            normalized_course["area"] = ""
                        
                        valid_courses.append(normalized_course)
                    
                    if valid_courses:
                        return valid_courses
                elif debug_info is not None:
                    debug_info["detected_format"] = "unknown"
        except (json.JSONDecodeError, IOError, OSError) as e:
            if debug_info is not None:
                debug_info["parse_error"] = str(e)
                debug_info["detected_format"] = "unknown"
            logger.error(f"Failed to load courses from {courses_path}: {str(e)}", exc_info=True)
    
    return []


async def fetch_weather_data(lat: float, lon: float, target_date: str):
    """
    Fetch weather data for a specific date.
    Returns dict with temperature, wind_speed, precipitation, sunset, or None if error.
    """
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,wind_speed_10m_max,precipitation_sum,sunset",
            "timezone": "auto",
            "start_date": target_date,
            "end_date": target_date
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            if "daily" in data and len(data["daily"]["time"]) > 0:
                daily = data["daily"]
                result = {
                    "temperature_max": daily["temperature_2m_max"][0],
                    "temperature_min": daily["temperature_2m_min"][0],
                    "wind_speed": daily["wind_speed_10m_max"][0],
                    "precipitation": daily["precipitation_sum"][0]
                }
                # Add sunset if available
                if "sunset" in daily and len(daily["sunset"]) > 0:
                    result["sunset"] = daily["sunset"][0]
                return result
    except Exception:
        pass
    
    return None


async def fetch_historical_rainfall(lat: float, lon: float, days: int = 7):
    """
    Fetch total rainfall for the last N days.
    Returns total rainfall in mm, or None if error.
    """
    try:
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days-1)
        
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "precipitation_sum",
            "timezone": "auto",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat()
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            if "daily" in data and "precipitation_sum" in data["daily"]:
                rainfall_values = data["daily"]["precipitation_sum"]
                total_rainfall = sum(rainfall_values) if rainfall_values else 0
                return total_rainfall
    except Exception:
        pass
    
    return None


async def fetch_tomorrow_weather(lat: float, lon: float):
    """Fetch weather for tomorrow for comparison."""
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    return await fetch_weather_data(lat, lon, tomorrow)


def is_weather_favorable(weather_rating: str) -> bool:
    """
    Determine if weather rating is favorable for golf.
    Returns True for "Dry", False for "Rainy", "Windy", "Cold", "Poor conditions"
    """
    return weather_rating == "Dry"


def calculate_weather_label(weather_data):
    """
    Derive weather label from forecast inputs using condition-based descriptors.
    Prioritizes: Precipitation > Wind > Temperature
    
    Returns dict with:
    - weather_label: Primary descriptor (Dry / Showers / Rain / Calm / Breezy / Windy / Mild / Cold / Very cold)
    - weather_rating: Fallback rating (Dry / Rainy / Windy / Cold / Poor conditions)
    - precipitation_label: Dry / Showers / Rain
    - wind_label: Calm / Breezy / Windy
    - temperature_label: Mild / Cold / Very cold (if available)
    - explanation: Plain English explanation of conditions
    """
    if not weather_data:
        return {
            "weather_label": "Dry",
            "weather_rating": "Dry",
            "precipitation_label": "Dry",
            "wind_label": "Calm",
            "temperature_label": "Mild",
            "explanation": "Dry conditions with calm winds and mild temperatures provide ideal playing conditions"
        }
    
    temp_avg = (weather_data["temperature_min"] + weather_data["temperature_max"]) / 2
    wind_speed = weather_data["wind_speed"]
    precipitation = weather_data["precipitation"]
    
    # Determine precipitation label (priority 1)
    if precipitation >= 5:
        precipitation_label = "Rain"
    elif precipitation >= 1:
        precipitation_label = "Showers"
    else:
        precipitation_label = "Dry"
    
    # Determine wind label (priority 2)
    if wind_speed >= 30:
        wind_label = "Windy"
    elif wind_speed >= 20:
        wind_label = "Breezy"
    else:
        wind_label = "Calm"
    
    # Determine temperature label (priority 3)
    if temp_avg < 5:
        temperature_label = "Very cold"
    elif temp_avg < 10:
        temperature_label = "Cold"
    else:
        temperature_label = "Mild"
    
    # Derive primary weather_label (prioritize precipitation > wind > temperature)
    if precipitation_label != "Dry":
        weather_label = precipitation_label  # Rain or Showers
    elif wind_label == "Windy":
        weather_label = "Windy"
    elif temperature_label == "Very cold":
        weather_label = "Very cold"
    elif temperature_label == "Cold":
        weather_label = "Cold"
    else:
        weather_label = "Dry"
    
    # Generate fallback weather_rating for backward compatibility
    if precipitation >= 5:
        weather_rating = "Rainy"
    elif wind_speed >= 30:
        weather_rating = "Windy"
    elif temp_avg < 10:
        weather_rating = "Cold"
    elif precipitation >= 1 or wind_speed >= 20 or temp_avg < 15:
        weather_rating = "Poor conditions"
    else:
        weather_rating = "Dry"
    
    # Generate explanation
    explanation_parts = []
    if precipitation_label == "Rain":
        explanation_parts.append("Heavy rain affects ball flight, visibility, and overall comfort")
    elif precipitation_label == "Showers":
        explanation_parts.append("Light showers may affect ball flight and visibility intermittently")
    elif precipitation_label == "Dry":
        explanation_parts.append("Dry conditions")
    
    if wind_label == "Windy":
        explanation_parts.append("strong winds significantly affect ball flight and distance control")
    elif wind_label == "Breezy":
        explanation_parts.append("breezy conditions may affect ball flight")
    else:
        explanation_parts.append("calm winds")
    
    if temperature_label == "Very cold":
        explanation_parts.append("very cold temperatures reduce ball flight distance and affect swing flexibility")
    elif temperature_label == "Cold":
        explanation_parts.append("cold temperatures reduce ball flight distance")
    else:
        explanation_parts.append("mild temperatures")
    
    explanation = ", ".join(explanation_parts) + "."
    explanation = explanation[0].upper() + explanation[1:]  # Capitalize first letter
    
    return {
        "weather_label": weather_label,
        "weather_rating": weather_rating,
        "precipitation_label": precipitation_label,
        "wind_label": wind_label,
        "temperature_label": temperature_label,
        "explanation": explanation
    }


def calculate_weather_rating(weather_data):
    """
    Calculate weather rating (backward compatibility wrapper).
    Returns weather_rating: Dry / Rainy / Windy / Cold / Poor conditions
    """
    weather_info = calculate_weather_label(weather_data)
    return weather_info["weather_rating"]


def calculate_ground_condition(historical_rainfall):
    """
    Calculate ground condition with score and explanation.
    Returns dict with:
    - ground_label: Firm / Normal / Soft / Too soft
    - ground_score: 0-100
    - explanation: One-sentence explanation of how it changes play
    """
    if historical_rainfall is None:
        # Default to Normal if no data available
        return {
            "ground_label": "Normal",
            "ground_score": 70,
            "explanation": "Normal ground conditions provide standard ball roll and predictable lies"
        }
    
    # Thresholds based on 7-day rainfall (mm)
    # Firm: < 5mm (fast, clean lies)
    # Normal: 5-15mm (standard conditions)
    # Soft: 15-35mm (playable but heavy)
    # Too soft: >= 35mm (likely to affect play)
    if historical_rainfall < 5:
        return {
            "ground_label": "Firm",
            "ground_score": 100,
            "explanation": "Firm ground provides fast roll and clean lies, making approach shots predictable with good carry distance"
        }
    elif historical_rainfall < 15:
        return {
            "ground_label": "Normal",
            "ground_score": 80,
            "explanation": "Normal ground conditions provide standard ball roll and predictable lies without significant impact on carry or footing"
        }
    elif historical_rainfall < 35:
        return {
            "ground_label": "Soft",
            "ground_score": 50,
            "explanation": "Soft ground reduces ball roll and makes footing less stable, requiring more club on approach shots and affecting recovery play"
        }
    else:  # >= 35mm
        return {
            "ground_label": "Too soft",
            "ground_score": 25,
            "explanation": "Very soft ground significantly reduces ball roll, increases plugging risk, and makes footing unstable, making approach shots and recovery play much harder"
        }


def calculate_tomorrow_forecast(today_weather, tomorrow_weather):
    """
    Compare today and tomorrow weather: Improving / Similar / Worse
    """
    if not today_weather or not tomorrow_weather:
        return None
    
    today_score = 0
    tomorrow_score = 0
    
    # Temperature comparison
    today_temp = (today_weather["temperature_min"] + today_weather["temperature_max"]) / 2
    tomorrow_temp = (tomorrow_weather["temperature_min"] + tomorrow_weather["temperature_max"]) / 2
    if 15 <= today_temp <= 25:
        today_score += 1
    if 15 <= tomorrow_temp <= 25:
        tomorrow_score += 1
    
    # Wind comparison
    if today_weather["wind_speed"] < 20:
        today_score += 1
    if tomorrow_weather["wind_speed"] < 20:
        tomorrow_score += 1
    
    # Precipitation comparison
    if today_weather["precipitation"] < 1:
        today_score += 1
    if tomorrow_weather["precipitation"] < 1:
        tomorrow_score += 1
    
    if tomorrow_score > today_score:
        return "Improving"
    elif tomorrow_score == today_score:
        return "Similar"
    else:
        return "Worse"


def get_sunset_time_fallback(month: int) -> str:
    """
    Get estimated sunset time by month for UK (London area).
    Returns time string in HH:MM format (24-hour).
    """
    # Approximate sunset times for London, UK by month
    sunset_times = {
        1: "16:30",  # January
        2: "17:15",  # February
        3: "18:00",  # March
        4: "19:45",  # April
        5: "20:30",  # May
        6: "21:15",  # June
        7: "21:00",  # July
        8: "20:15",  # August
        9: "19:00",  # September
        10: "17:45", # October
        11: "16:30", # November
        12: "16:00"  # December
    }
    return sunset_times.get(month, "18:00")  # Default to 18:00


def get_tee_time_from_time_of_day(time_of_day: str) -> str:
    """
    Convert time_of_day to estimated tee time.
    Returns time string in HH:MM format (24-hour).
    """
    tee_times = {
        "Morning": "09:00",
        "Midday": "12:00",
        "Afternoon": "14:30",
        "Evening": "16:30"
    }
    return tee_times.get(time_of_day, "12:00")


def calculate_daylight_feasibility(time_of_day: str, busyness_rating: str, weather_data: dict, month: int, target_date: str) -> dict:
    """
    Calculate daylight feasibility and recommended holes.
    Returns dict with:
    - recommended_holes: 18 or 9
    - daylight_label: "Plenty of light", "Tight", or "Not feasible"
    - daylight_minutes: minutes of daylight available
    """
    # Get tee time
    tee_time_str = get_tee_time_from_time_of_day(time_of_day)
    tee_hour, tee_minute = map(int, tee_time_str.split(":"))
    
    # Get sunset time (from weather data if available, otherwise fallback)
    sunset_time_str = None
    if weather_data and "sunset" in weather_data:
        # Parse ISO format sunset time (e.g., "2024-01-15T16:30:00Z")
        try:
            sunset_dt = datetime.fromisoformat(weather_data["sunset"].replace("Z", "+00:00"))
            sunset_time_str = sunset_dt.strftime("%H:%M")
        except Exception:
            pass
    
    if not sunset_time_str:
        sunset_time_str = get_sunset_time_fallback(month)
    
    sunset_hour, sunset_minute = map(int, sunset_time_str.split(":"))
    
    # Calculate daylight minutes
    # Parse target date to get the actual date
    target_dt = datetime.fromisoformat(target_date)
    tee_datetime = target_dt.replace(hour=tee_hour, minute=tee_minute)
    sunset_datetime = target_dt.replace(hour=sunset_hour, minute=sunset_minute)
    
    # Handle case where sunset is next day (shouldn't happen for UK, but handle it)
    if sunset_datetime < tee_datetime:
        sunset_datetime += timedelta(days=1)
    
    daylight_minutes = int((sunset_datetime - tee_datetime).total_seconds() / 60)
    
    # Estimate expected duration based on busyness
    duration_map = {
        "Quiet": {"18": 240, "9": 120},
        "Moderate": {"18": 270, "9": 135},
        "Busy": {"18": 300, "9": 150},
        "Very busy": {"18": 300, "9": 150}  # Treat Very busy same as Busy
    }
    durations = duration_map.get(busyness_rating, {"18": 270, "9": 135})
    duration_18 = durations["18"]
    duration_9 = durations["9"]
    
    # Determine recommended holes
    recommended_holes = 9  # Default
    daylight_label = "Not feasible"
    
    if daylight_minutes >= duration_18:
        recommended_holes = 18
        margin = daylight_minutes - duration_18
        if margin >= 30:
            daylight_label = "Plenty of light"
        else:
            daylight_label = "Tight"
    elif daylight_minutes >= duration_9:
        recommended_holes = 9
        margin = daylight_minutes - duration_9
        if margin >= 30:
            daylight_label = "Plenty of light"
        else:
            daylight_label = "Tight"
    else:
        recommended_holes = 9
        daylight_label = "Not feasible"
    
    # Calculate finish time estimate
    expected_duration = duration_18 if recommended_holes == 18 else duration_9
    finish_datetime = tee_datetime + timedelta(minutes=expected_duration)
    finish_time_estimate = finish_datetime.strftime("%H:%M")
    
    return {
        "recommended_holes": recommended_holes,
        "daylight_label": daylight_label,
        "daylight_minutes": daylight_minutes,
        "finish_time_estimate": finish_time_estimate,
        "sunset_time": sunset_time_str
    }


def validate_and_filter_reasons(reasons, weather_label, ground_label, busyness_label, course_difficulty, handicap_band=None):
    """
    Validate and filter reasons to prevent duplicates and generic filler.
    
    Rules:
    - reasons must be unique strings (by impact text)
    - reasons array length max 3
    - each reason must reference at least one of: weather_label, ground_label, busyness_label, difficulty, handicap band
    - if a reason cannot cite a factor, drop it
    
    Returns: filtered list of reason dicts
    """
    if not reasons:
        return []
    
    # Extract required factor names for validation
    required_factors = {
        "weather": weather_label.lower(),
        "ground": ground_label.lower(),
        "busyness": busyness_label.lower(),
        "difficulty": course_difficulty.lower(),
    }
    if handicap_band:
        required_factors["handicap"] = handicap_band.lower()
    
    # Track seen impact strings for uniqueness
    seen_impacts = set()
    validated_reasons = []
    
    for reason in reasons:
        # Extract impact text (handle both dict and string formats)
        if isinstance(reason, dict):
            impact_text = reason.get("impact", "")
            # If no impact field, try to construct from other fields
            if not impact_text:
                if "condition" in reason:
                    impact_text = reason.get("condition", "")
                elif "factor" in reason:
                    impact_text = reason.get("factor", "")
        else:
            impact_text = str(reason)
        
        # Normalize for comparison (lowercase, strip whitespace)
        impact_normalized = impact_text.lower().strip()
        
        # Skip if duplicate
        if impact_normalized in seen_impacts:
            continue
        
        # Skip if empty
        if not impact_text or not impact_normalized:
            continue
        
        # Check if reason references at least one required factor
        references_factor = False
        impact_lower = impact_text.lower()
        
        # Check for weather references
        if required_factors["weather"] in impact_lower or "weather" in impact_lower or weather_label.lower() in impact_lower:
            references_factor = True
        
        # Check for ground references
        if required_factors["ground"] in impact_lower or "ground" in impact_lower or ground_label.lower() in impact_lower:
            references_factor = True
        
        # Check for busyness references
        if required_factors["busyness"] in impact_lower or "busy" in impact_lower or busyness_label.lower() in impact_lower:
            references_factor = True
        
        # Check for difficulty references
        if required_factors["difficulty"] in impact_lower or "course" in impact_lower or "difficulty" in impact_lower:
            references_factor = True
        
        # Check for handicap references
        if handicap_band:
            if handicap_band.lower() in impact_lower or "handicap" in impact_lower or "beginner" in impact_lower or "low handicap" in impact_lower or "mid handicap" in impact_lower or "high handicap" in impact_lower:
                references_factor = True
        
        # Skip if doesn't reference any factor
        if not references_factor:
            continue
        
        # Add to validated list
        seen_impacts.add(impact_normalized)
        validated_reasons.append(reason)
        
        # Stop at max 3 reasons
        if len(validated_reasons) >= 3:
            break
    
    return validated_reasons


def compute_playability(weather_data, ground_info, busyness_info, course_difficulty, daylight_label, handicap, recommended_holes, price_tier):
    """
    Compute playability using deterministic scoring model with explicit thresholds.
    Returns dict with:
    - overall_score (0-100)
    - verdict (Play / Not ideal) - binary decision only
    - reasons[] (each reason references specific factor + threshold)
    - recommendations[] (handicap-aware)
    - factor_scores{} (individual factor scores)
    - weather_info{} (weather label and details)
    """
    reasons = []
    recommendations = []
    factor_scores = {}
    
    # Calculate weather label and info
    weather_info = calculate_weather_label(weather_data)
    weather_label = weather_info["weather_label"]
    weather_rating = weather_info["weather_rating"]  # For backward compatibility
    
    # Daylight feasibility check (overrides everything if not feasible)
    daylight_feasible = daylight_label != "Not feasible"
    if not daylight_feasible:
        factor_scores["daylight"] = 0
        reasons.append({
            "factor": "daylight",
            "condition": daylight_label,
            "threshold": "Not feasible",
            "impact": "Cannot complete round safely before sunset"
        })
    elif daylight_label == "Tight":
        factor_scores["daylight"] = 50  # Informational only
        reasons.append({
            "factor": "daylight",
            "condition": daylight_label,
            "threshold": "Tight",
            "impact": f"Daylight is tight for {recommended_holes} holes, requiring good pace"
        })
    else:  # Plenty of light
        factor_scores["daylight"] = 100  # Informational only
        reasons.append({
            "factor": "daylight",
            "condition": daylight_label,
            "threshold": "Plenty of light",
            "impact": f"Sufficient daylight for {recommended_holes} holes"
        })
    
    # Weather factor scoring with explicit thresholds (0-100)
    # Map weather_label to score: Dry=100, Showers=50, Rain=25, Windy=40, Breezy=70, Very cold=30, Cold=60, Poor conditions=15
    if weather_label == "Dry":
        factor_scores["weather"] = 100
        threshold_hit = "Dry (100)"
    elif weather_label == "Showers":
        factor_scores["weather"] = 50
        threshold_hit = "Showers (50)"
    elif weather_label == "Rain":
        factor_scores["weather"] = 25
        threshold_hit = "Rain (25)"
    elif weather_label == "Windy":
        factor_scores["weather"] = 40
        threshold_hit = "Windy (40)"
    elif weather_label == "Breezy":
        factor_scores["weather"] = 70
        threshold_hit = "Breezy (70)"
    elif weather_label == "Very cold":
        factor_scores["weather"] = 30
        threshold_hit = "Very cold (30)"
    elif weather_label == "Cold":
        factor_scores["weather"] = 60
        threshold_hit = "Cold (60)"
    else:  # Poor conditions (fallback)
        factor_scores["weather"] = 15
        threshold_hit = "Poor conditions (15)"
    
    # Use explanation from weather_info
    impact = weather_info["explanation"]
    
    reasons.append({
        "factor": "weather",
        "condition": weather_label,
        "threshold": threshold_hit,
        "impact": impact
    })
    
    # Ground condition factor scoring - use ground_info dict
    # Extract ground_label, ground_score, and explanation from ground_info
    if isinstance(ground_info, dict):
        ground_label = ground_info["ground_label"]
        factor_scores["ground"] = ground_info["ground_score"]
        impact = ground_info["explanation"]
    else:
        # Backward compatibility: if it's a string, use defaults
        ground_label = ground_info
        if ground_label == "Firm":
            factor_scores["ground"] = 100
            impact = "Firm ground provides fast roll and clean lies, making approach shots predictable with good carry distance"
        elif ground_label == "Normal":
            factor_scores["ground"] = 80
            impact = "Normal ground conditions provide standard ball roll and predictable lies without significant impact on carry or footing"
        elif ground_label == "Soft":
            factor_scores["ground"] = 50
            impact = "Soft ground reduces ball roll and makes footing less stable, requiring more club on approach shots and affecting recovery play"
        else:  # Too soft
            factor_scores["ground"] = 25
            impact = "Very soft ground significantly reduces ball roll, increases plugging risk, and makes footing unstable, making approach shots and recovery play much harder"
    
    threshold_hit = f"{ground_label} ({factor_scores['ground']})"
    
    reasons.append({
        "factor": "ground",
        "condition": ground_label,
        "threshold": threshold_hit,
        "impact": impact
    })
    
    # Busyness factor scoring - use busyness_info dict
    # Extract busyness_label, busyness_score, and explanation from busyness_info
    if isinstance(busyness_info, dict):
        busyness_label = busyness_info["busyness_label"]
        factor_scores["busyness"] = busyness_info["busyness_score"]
        impact = busyness_info["explanation"]
    else:
        # Backward compatibility: if it's a string, use defaults
        busyness_label = busyness_info
        if busyness_label == "Quiet":
            factor_scores["busyness"] = 100
            impact = "Quiet conditions mean minimal waiting between shots and good pace of play"
        elif busyness_label == "Moderate":
            factor_scores["busyness"] = 70
            impact = "Moderate conditions mean occasional waiting between shots but generally good pace of play"
        elif busyness_label == "Busy":
            factor_scores["busyness"] = 40
            impact = "Busy conditions mean some waiting between shots and slower pace of play"
        else:  # Very busy
            factor_scores["busyness"] = 20
            impact = "Very busy conditions mean significant waiting between shots, slower pace of play, and potential start delays"
    
    threshold_hit = f"{busyness_label} ({factor_scores['busyness']})"
    
    reasons.append({
        "factor": "busyness",
        "condition": busyness_label,
        "threshold": threshold_hit,
        "impact": impact
    })
    
    # Handicap suitability factor scoring with explicit handicap bands and condition-based penalties
    suitability_result = calculate_handicap_suitability_score(
        handicap, course_difficulty, ground_info, weather_label, busyness_info
    )
    
    factor_scores["suitability"] = suitability_result["suitability_score"]
    suitability_label = suitability_result["suitability_label"]
    suitability_reasons = suitability_result["reasons"]
    
    # Add suitability reasons to main reasons list
    for reason_text in suitability_reasons:
        reasons.append({
            "factor": "suitability",
            "condition": suitability_label,
            "threshold": f"Handicap {handicap} ({suitability_result['band']} band)",
            "impact": reason_text
        })
    
    # Price factor scoring with explicit thresholds (0-100, informational only)
    # Thresholds: Low=100, Mid=70, High=40
    # Note: Price has 0% weight but is scored for display/debugging
    if price_tier == "£":
        factor_scores["price"] = 100
        threshold_hit = "Low (100)"
    elif price_tier == "££":
        factor_scores["price"] = 70
        threshold_hit = "Mid (70)"
    else:  # £££
        factor_scores["price"] = 40
        threshold_hit = "High (40)"
    
    # Price doesn't add to reasons unless extreme (informational only)
    
    # Calculate weighted overall score
    # Weights: Weather 30%, Ground 25%, Busyness 20%, Suitability 25%, Price 0%
    weights = {
        "weather": 0.30,
        "ground": 0.25,
        "busyness": 0.20,
        "suitability": 0.25,
        "price": 0.0  # Display only
    }
    
    # If daylight is not feasible, overall score is 0 (override)
    if not daylight_feasible:
        overall_score = 0
    else:
        overall_score = sum(factor_scores[factor] * weights[factor] for factor in weights if factor != "daylight")
        overall_score = int(overall_score)
    
    # Determine verdict from overall score with explicit thresholds (binary decision)
    # Play: >=60, Not ideal: <60
    if overall_score >= 60:
        verdict = "Play"
    else:
        verdict = "Not ideal"
    
    # Generate handicap-aware recommendations based on factor thresholds
    if verdict == "Play":
        # Play recommendations based on threshold scores
        if factor_scores["weather"] >= 100 and factor_scores["suitability"] >= 80:
            recommendations.append({
                "action": "Consider a personal best attempt",
                "reason": f"With dry weather and conditions well suited to your handicap of {handicap}, this is a good day for a personal best attempt"
            })
        elif factor_scores["busyness"] >= 70:
            recommendations.append({
                "action": "Any time window should work well",
                "reason": f"Course pressure is {busyness_label.lower()}, so any time window should work well without long waits"
            })
        else:
            recommendations.append({
                "action": "Plan for a social round",
                "reason": f"With {weather_label.lower()} weather and {busyness_label.lower()} course pressure, this is good for a social round"
            })
    else:  # Not ideal
        # Recommendations based on lowest scoring factors (threshold-based)
        if not daylight_feasible:
            recommendations.append({
                "action": "Try booking an earlier tee time tomorrow",
                "reason": f"With your handicap of {handicap}, finishing in daylight is important to avoid rushed shots and maintain proper form"
            })
        elif factor_scores["weather"] < 50:  # Threshold: weather score below 50
            if weather_label in ["Rain", "Showers"]:
                recommendations.append({
                    "action": "Check tomorrow's forecast for better conditions",
                    "reason": f"{weather_label.lower()} conditions affect ball flight more significantly for players with your handicap of {handicap}, making it harder to judge distances and control shots"
                })
            elif weather_label == "Windy":
                recommendations.append({
                    "action": "Check tomorrow's forecast for better conditions",
                    "reason": f"Windy conditions affect ball flight more significantly for players with your handicap of {handicap}, making it harder to judge distances and control shots"
                })
            elif weather_label in ["Cold", "Very cold"]:
                recommendations.append({
                    "action": "Check tomorrow's forecast for better conditions",
                    "reason": f"{weather_label.lower()} conditions affect ball flight distance and flexibility more significantly for players with your handicap of {handicap}, making it harder to maintain consistent swing tempo"
                })
        elif factor_scores["ground"] < 50:  # Threshold: ground score below 50
            recommendations.append({
                "action": "Consider waiting for firmer ground conditions",
                "reason": f"Softer ground penalises players with your handicap of {handicap} more heavily, as approach shots won't roll or bounce as expected, making distance control harder"
            })
        elif factor_scores["busyness"] < 50:  # Threshold: busyness score below 50
            recommendations.append({
                "action": "Try booking at a quieter time, like early morning or late afternoon",
                "reason": f"Busier conditions reduce waiting and help players with your handicap of {handicap} maintain tempo and rhythm between shots"
            })
        elif factor_scores["suitability"] < 50:  # Threshold: suitability score below 50
            recommendations.append({
                "action": "Consider trying a less demanding course today",
                "reason": f"Course difficulty combined with today's conditions adds unnecessary challenge for players with your handicap of {handicap}"
            })
    
    # Validate and filter reasons to prevent duplicates and generic filler
    handicap_band = suitability_result.get("band")
    reasons = validate_and_filter_reasons(
        reasons, weather_label, ground_label, busyness_label, course_difficulty, handicap_band
    )
    
    return {
        "overall_score": overall_score,
        "verdict": verdict,
        "reasons": reasons,
        "recommendations": recommendations,
        "factor_scores": factor_scores,
        "weather_info": weather_info,
        "weather_rating": weather_rating  # For backward compatibility
    }


def calculate_course_pressure(month, weather_rating, day_of_week, time_of_day, popularity_tier):
    """
    Calculate course pressure (busyness) with score and explanation.
    Returns dict with:
    - busyness_label: Quiet / Moderate / Busy / Very busy
    - busyness_score: 0-100
    - explanation: What the golfer should expect (waiting, pace, start delays)
    """
    # Base score from popularity tier (if only popularity_tier exists)
    base_score = 0
    if popularity_tier == "Low":
        base_score = 20  # Quiet baseline
    elif popularity_tier == "Medium":
        base_score = 50  # Moderate baseline
    elif popularity_tier == "High":
        base_score = 70  # Busy baseline
    else:
        base_score = 50  # Default to Moderate
    
    # Adjustments based on factors
    adjustments = 0
    
    # Seasonality (UK golf season: April-September peak)
    if month in [4, 5, 6, 7, 8, 9]:
        adjustments += 15  # Peak season
    elif month in [3, 10]:
        adjustments += 8  # Shoulder season
    
    # Weather attractiveness
    if is_weather_favorable(weather_rating):
        adjustments += 15  # Good weather attracts more players
    elif weather_rating == "Poor conditions":
        adjustments += 5  # Poor weather reduces pressure slightly
    
    # Day of week (weekend = busier)
    is_weekend = day_of_week in [5, 6]  # Saturday, Sunday
    is_friday = day_of_week == 4
    
    if is_weekend:
        adjustments += 20  # Weekend significantly busier
    elif is_friday:
        adjustments += 10  # Friday moderately busier
    
    # Time of day (peak times are busier)
    is_peak_time = False
    if time_of_day == "Midday":
        adjustments += 15
        is_peak_time = True
    elif time_of_day == "Afternoon":
        adjustments += 12
        is_peak_time = True
    elif time_of_day == "Morning":
        # Weekend mornings are peak times
        if is_weekend:
            adjustments += 18
            is_peak_time = True
        else:
            adjustments += 5
    # Evening is typically quieter
    elif time_of_day == "Evening":
        adjustments -= 5
    
    # Peak time penalty: weekend mornings get extra penalty
    if is_weekend and time_of_day == "Morning":
        adjustments += 10  # Additional penalty for weekend mornings
    
    # Calculate final score (0-100)
    busyness_score = base_score + adjustments
    busyness_score = max(0, min(100, busyness_score))
    
    # Determine label based on score
    if busyness_score >= 80:
        busyness_label = "Very busy"
    elif busyness_score >= 60:
        busyness_label = "Busy"
    elif busyness_score >= 40:
        busyness_label = "Moderate"
    else:
        busyness_label = "Quiet"
    
    # Generate explanation based on label and conditions
    if busyness_label == "Very busy":
        if is_weekend and is_peak_time:
            explanation = "Very busy conditions mean significant waiting between shots, slower pace of play, and potential start delays, especially on weekend peak times"
        elif is_weekend:
            explanation = "Very busy conditions mean significant waiting between shots, slower pace of play, and potential start delays on weekends"
        elif is_peak_time:
            explanation = "Very busy conditions mean significant waiting between shots, slower pace of play, and potential start delays during peak times"
        else:
            explanation = "Very busy conditions mean significant waiting between shots, slower pace of play, and potential start delays"
    elif busyness_label == "Busy":
        if is_weekend:
            explanation = "Busy conditions mean some waiting between shots and slower pace of play, with possible start delays on weekends"
        elif is_peak_time:
            explanation = "Busy conditions mean some waiting between shots and slower pace of play during peak times"
        else:
            explanation = "Busy conditions mean some waiting between shots and slower pace of play"
    elif busyness_label == "Moderate":
        explanation = "Moderate conditions mean occasional waiting between shots but generally good pace of play"
    else:  # Quiet
        explanation = "Quiet conditions mean minimal waiting between shots and good pace of play"
    
    return {
        "busyness_label": busyness_label,
        "busyness_score": busyness_score,
        "explanation": explanation
    }


def calculate_busyness_rating(month, weather_rating, day_of_week, time_of_day, popularity_tier):
    """
    Calculate busyness rating (backward compatibility wrapper).
    Returns busyness_label: Quiet / Moderate / Busy / Very busy
    """
    pressure_info = calculate_course_pressure(month, weather_rating, day_of_week, time_of_day, popularity_tier)
    return pressure_info["busyness_label"]


def calculate_handicap_suitability_score(handicap, course_difficulty, ground_info, weather_label, busyness_info):
    """
    Calculate handicap suitability score (0-100) with explicit handicap bands and condition-based penalties/bonuses.
    
    Handicap bands:
    - 0-9: Low
    - 10-18: Mid
    - 19-28: High
    - 29-54: Beginner
    
    Returns dict with:
    - suitability_score (0-100)
    - suitability_label (e.g., "Challenging for a 25 handicap today")
    - reasons[] (1-2 plain English reasons specific to handicap band)
    """
    # Extract ground_label from ground_info
    if isinstance(ground_info, dict):
        ground_label = ground_info["ground_label"]
    else:
        # Backward compatibility: if it's a string, use it
        ground_label = ground_info
    
    # Extract busyness_label from busyness_info
    if isinstance(busyness_info, dict):
        busyness_label = busyness_info["busyness_label"]
    else:
        # Backward compatibility: if it's a string, use it
        busyness_label = busyness_info
    
    # Determine handicap band
    if handicap <= 9:
        band = "Low"
        band_description = "low handicap"
    elif handicap <= 18:
        band = "Mid"
        band_description = "mid handicap"
    elif handicap <= 28:
        band = "High"
        band_description = "high handicap"
    else:  # 29-54
        band = "Beginner"
        band_description = "beginner handicap"
    
    # Start with base score based on course difficulty
    # Easy courses are better for all handicaps, Hard courses penalize higher handicaps more
    if course_difficulty == "Easy":
        base_score = 80
    elif course_difficulty == "Medium":
        base_score = 60
    else:  # Hard
        base_score = 40
    
    # Apply handicap band penalties/bonuses
    # Higher handicaps get penalized more on harder courses
    if course_difficulty == "Hard":
        if band == "Low":
            handicap_penalty = 0  # Low handicaps handle hard courses well
        elif band == "Mid":
            handicap_penalty = -15
        elif band == "High":
            handicap_penalty = -25
        else:  # Beginner
            handicap_penalty = -35
    elif course_difficulty == "Medium":
        if band == "Low":
            handicap_penalty = 0
        elif band == "Mid":
            handicap_penalty = -5
        elif band == "High":
            handicap_penalty = -15
        else:  # Beginner
            handicap_penalty = -25
    else:  # Easy
        if band == "Low":
            handicap_penalty = 0
        elif band == "Mid":
            handicap_penalty = 5  # Bonus for mid handicaps on easy courses
        elif band == "High":
            handicap_penalty = 10  # Bonus for high handicaps on easy courses
        else:  # Beginner
            handicap_penalty = 15  # Bonus for beginners on easy courses
    
    # Apply ground condition penalties
    # Soft ground penalizes higher handicaps more (affects approach shots and recovery)
    ground_penalty = 0
    if ground_label == "Too soft":
        if band == "Low":
            ground_penalty = -10
        elif band == "Mid":
            ground_penalty = -20
        elif band == "High":
            ground_penalty = -30
        else:  # Beginner
            ground_penalty = -35
    elif ground_label == "Soft":
        if band == "Low":
            ground_penalty = -5
        elif band == "Mid":
            ground_penalty = -10
        elif band == "High":
            ground_penalty = -20
        else:  # Beginner
            ground_penalty = -25
    elif ground_label == "Normal":
        # Normal conditions: no penalty
        ground_penalty = 0
    # Firm ground: no penalty (or small bonus for higher handicaps)
    elif ground_label == "Firm":
        if band in ["High", "Beginner"]:
            ground_penalty = 5  # Bonus for predictable bounce
    
    # Apply weather penalties based on condition-based labels
    # Poor weather penalizes higher handicaps more (affects ball flight and distance control)
    weather_penalty = 0
    if weather_label == "Rain":
        if band == "Low":
            weather_penalty = -5
        elif band == "Mid":
            weather_penalty = -15
        elif band == "High":
            weather_penalty = -25
        else:  # Beginner
            weather_penalty = -30
    elif weather_label == "Showers":
        if band == "Low":
            weather_penalty = -3
        elif band == "Mid":
            weather_penalty = -10
        elif band == "High":
            weather_penalty = -20
        else:  # Beginner
            weather_penalty = -25
    elif weather_label == "Windy":
        if band == "Low":
            weather_penalty = -5
        elif band == "Mid":
            weather_penalty = -10
        elif band == "High":
            weather_penalty = -20
        else:  # Beginner
            weather_penalty = -25
    elif weather_label == "Very cold":
        if band == "Low":
            weather_penalty = -5
        elif band == "Mid":
            weather_penalty = -10
        elif band == "High":
            weather_penalty = -20
        else:  # Beginner
            weather_penalty = -25
    elif weather_label == "Cold":
        if band == "Low":
            weather_penalty = -3
        elif band == "Mid":
            weather_penalty = -8
        elif band == "High":
            weather_penalty = -15
        else:  # Beginner
            weather_penalty = -20
    elif weather_label == "Dry":
        # Dry weather is good for all, bonus for higher handicaps
        if band in ["High", "Beginner"]:
            weather_penalty = 5
    else:  # Poor conditions or other
        if band == "Low":
            weather_penalty = -10
        elif band == "Mid":
            weather_penalty = -20
        elif band == "High":
            weather_penalty = -30
        else:  # Beginner
            weather_penalty = -35
    
    # Apply busyness penalties
    # Busy conditions penalize higher handicaps more (waiting harms rhythm/pace)
    busyness_penalty = 0
    if busyness_label == "Very busy":
        if band == "Low":
            busyness_penalty = -5
        elif band == "Mid":
            busyness_penalty = -15
        elif band == "High":
            busyness_penalty = -25
        else:  # Beginner
            busyness_penalty = -30
    elif busyness_label == "Busy":
        if band == "Low":
            busyness_penalty = -3
        elif band == "Mid":
            busyness_penalty = -10
        elif band == "High":
            busyness_penalty = -20
        else:  # Beginner
            busyness_penalty = -25
    elif busyness_label == "Moderate":
        if band in ["High", "Beginner"]:
            busyness_penalty = -5
    # Quiet: no penalty (or small bonus for higher handicaps)
    elif busyness_label == "Quiet":
        if band in ["High", "Beginner"]:
            busyness_penalty = 5  # Bonus for rhythm-friendly conditions
    
    # Calculate final score
    suitability_score = base_score + handicap_penalty + ground_penalty + weather_penalty + busyness_penalty
    suitability_score = max(0, min(100, suitability_score))  # Clamp to 0-100
    
    # Generate suitability label based on score (binary decision)
    # Use normalized format: "Good for a {handicap} handicap" or "Tough for a {handicap} handicap today"
    if suitability_score >= 60:
        suitability_label = f"Good for a {handicap} handicap"
    else:
        suitability_label = f"Tough for a {handicap} handicap today"
    
    # Generate 1-2 plain English reasons specific to handicap band
    reasons = []
    
    # Primary reason: course difficulty + handicap band interaction
    if course_difficulty == "Hard" and band in ["High", "Beginner"]:
        reasons.append(f"A {course_difficulty.lower()} course combined with your {band_description} ({handicap}) makes approach shots and recovery play significantly harder")
    elif course_difficulty == "Easy" and band in ["High", "Beginner"]:
        reasons.append(f"An {course_difficulty.lower()} course suits your {band_description} ({handicap}), allowing you to play your natural game without unnecessary pressure")
    elif course_difficulty == "Hard" and band == "Mid":
        reasons.append(f"A {course_difficulty.lower()} course adds challenge for your {band_description} ({handicap}), requiring more precise shot placement")
    
    # Secondary reason: condition-specific (prioritize worst condition)
    if ground_label in ["Too soft", "Soft"] and band in ["High", "Beginner"]:
        reasons.append(f"{ground_label.lower()} ground conditions penalise {band_description} players more heavily, as approach shots won't roll or bounce as expected")
    elif weather_label in ["Rain", "Showers", "Windy", "Very cold", "Cold"] and band in ["High", "Beginner"]:
        reasons.append(f"{weather_label.lower()} conditions affect ball flight and distance control more significantly for {band_description} players ({handicap}), making it harder to judge shots")
    elif busyness_label in ["Busy", "Very busy"] and band in ["High", "Beginner"]:
        reasons.append(f"{busyness_label.lower()} conditions increase waiting between shots, which disrupts rhythm and pace more for {band_description} players ({handicap})")
    
    # Ensure we have at least one reason
    if not reasons:
        if suitability_score >= 60:
            reasons.append(f"Course conditions are well matched to your {band_description} ({handicap})")
        else:
            reasons.append(f"Course conditions add unnecessary challenge for your {band_description} ({handicap})")
    
    # Deduplicate and limit to 2 reasons
    seen_reasons = set()
    unique_reasons = []
    for reason in reasons:
        reason_normalized = reason.lower().strip()
        if reason_normalized and reason_normalized not in seen_reasons:
            seen_reasons.add(reason_normalized)
            unique_reasons.append(reason)
        if len(unique_reasons) >= 2:
            break
    reasons = unique_reasons
    
    return {
        "suitability_score": suitability_score,
        "suitability_label": suitability_label,
        "reasons": reasons,
        "band": band
    }


def calculate_handicap_suitability(handicap, course_difficulty, busyness_rating):
    """
    Calculate handicap suitability: Well suited / Not ideal today (binary decision)
    Handicap buckets: 0-9, 10-19, 20-28, 29+
    Principle: busy + hard course = less suitable for higher handicaps
    """
    # Determine handicap bucket
    if handicap <= 9:
        handicap_bucket = "low"
    elif handicap <= 19:
        handicap_bucket = "mid"
    elif handicap <= 28:
        handicap_bucket = "high"
    else:
        handicap_bucket = "very_high"
    
    # Base suitability on difficulty
    if course_difficulty == "Easy":
        base_suitability = 3
    elif course_difficulty == "Medium":
        base_suitability = 2
    else:  # Hard
        base_suitability = 1
    
    # Adjust for handicap bucket
    if handicap_bucket == "low":
        handicap_adjustment = 0
    elif handicap_bucket == "mid":
        handicap_adjustment = -1
    elif handicap_bucket == "high":
        handicap_adjustment = -2
    else:  # very_high
        handicap_adjustment = -3
    
    # Adjust for busyness (busier = less suitable for higher handicaps)
    if busyness_rating == "Very busy":
        busyness_adjustment = -2
    elif busyness_rating == "Busy":
        busyness_adjustment = -1
    else:
        busyness_adjustment = 0
    
    # Apply penalty more strongly for higher handicaps
    if handicap_bucket in ["high", "very_high"]:
        busyness_adjustment *= 2
    
    total_score = base_suitability + handicap_adjustment + busyness_adjustment
    
    if total_score >= 1:
        return "Well suited"
    else:
        return "Not ideal today"


def determine_play_recommendation(weather_rating, ground_condition, busyness_rating, handicap_suitability, daylight_label=None):
    """
    Determine Play or Don't play recommendation.
    If daylight_label is "Not feasible", verdict must be "Don't play".
    """
    # Daylight feasibility overrides everything
    if daylight_label == "Not feasible":
        return "Don't play"
    
    score = 0
    
    if is_weather_favorable(weather_rating):
        score += 2
    elif weather_rating == "Poor conditions":
        # Poor conditions but not severe (Rainy/Windy/Cold)
        score += 1
    
    if ground_condition in ["Firm", "Mixed"]:
        score += 1
    
    if busyness_rating in ["Quiet", "Moderate"]:
        score += 2
    elif busyness_rating == "Busy":
        score += 1
    
    if handicap_suitability == "Well suited":
        score += 2
    
    return "Play" if score >= 5 else "Don't play"


def generate_added_action(play_recommendation, time_of_day, busyness_rating, weather_rating, handicap_suitability):
    """
    Generate added action suggestions.
    If Play: suggest best time window and social vs PB attempt
    If Don't play: suggest tomorrow or quieter time and practical alternative
    """
    if play_recommendation == "Play":
        # Suggest best time window with explanation
        if busyness_rating in ["Quiet", "Moderate"]:
            time_suggestion = f"Course busyness is {busyness_rating.lower()}, so any time window should work well without long waits"
        elif time_of_day == "Morning":
            time_suggestion = f"Course busyness is {busyness_rating.lower()}, but morning typically offers quieter conditions than midday or afternoon"
        elif time_of_day == "Evening":
            time_suggestion = f"Course busyness is {busyness_rating.lower()}, but evening typically offers quieter conditions than midday or afternoon"
        else:
            time_suggestion = f"Course busyness is {busyness_rating.lower()}, so consider Morning or Evening for quieter conditions"
        
        # Social vs PB attempt with explanation
        if is_weather_favorable(weather_rating) and handicap_suitability == "Well suited":
            round_type = f"With {weather_rating.lower()} weather and course conditions well suited to your handicap, this is a good day for a personal best attempt"
        elif is_weather_favorable(weather_rating):
            round_type = f"{weather_rating.lower().capitalize()} weather provides predictable ball flight, making this good for a social round"
        else:
            round_type = f"{weather_rating.lower().capitalize()} weather affects ball flight, so this is better suited for a social round rather than a personal best attempt"
        
        return f"{time_suggestion}. {round_type}."
    else:
        # Don't play suggestions - these are now handled in generate_what_to_do
        # This function is deprecated for "Don't play" case
        return ""


def generate_why_bullets(play_recommendation, weather_rating, ground_condition, busyness_rating, handicap_suitability, daylight_label, handicap):
    """
    Generate exactly 3 bullet points explaining why the verdict was given.
    Each bullet explains WHAT is happening and WHY it matters.
    Returns a list of 3 strings.
    """
    bullets = []
    
    # Priority order: daylight, weather, ground, busyness, handicap
    # Always include the most significant factors
    
    # Daylight explanations
    if daylight_label == "Not feasible":
        bullets.append("Not enough daylight to complete your round safely, which means you'll likely finish in darkness")
    elif daylight_label == "Tight":
        bullets.append("Daylight is tight for your planned round, which means you'll need to maintain a good pace to finish before sunset")
    
    # Weather explanations - explain WHAT and WHY
    if weather_rating == "Rainy":
        bullets.append("Rainy conditions today will affect ball flight, visibility, and overall comfort during your round, making it harder to judge distances and control shots")
    elif weather_rating == "Windy":
        bullets.append("Windy conditions today will significantly affect ball flight and distance control, making it harder to judge where your shots will land")
    elif weather_rating == "Cold":
        bullets.append("Cold conditions today will affect ball flight distance and make it harder to maintain flexibility and feel in your swing")
    elif weather_rating == "Poor conditions":
        bullets.append("Poor weather conditions today will affect ball flight, visibility, and overall comfort during your round")
    elif weather_rating == "Dry":
        bullets.append("Dry weather conditions today provide ideal ball flight and comfortable playing conditions")
    
    # Ground condition explanations - explain WHAT and WHY
    if ground_condition == "Soggy":
        bullets.append("Recent rain has left the ground very wet, which makes longer approaches and recovery shots harder as the ball won't roll or bounce as expected")
    elif ground_condition == "Soft":
        bullets.append("Soft ground conditions from recent rain make approach shots and recovery shots more difficult, as the ball won't get the bounce or roll you might expect")
    elif ground_condition == "Firm":
        bullets.append("Firm ground conditions provide good ball roll and predictable bounce, making approach shots and recovery shots easier")
    elif ground_condition == "Mixed":
        bullets.append("Mixed ground conditions mean some areas will be firmer than others, requiring you to adapt your approach shots throughout the round")
    
    # Busyness explanations - explain WHAT and WHY
    if busyness_rating in ["Very busy", "Busy"]:
        bullets.append(f"Busier tee times today ({busyness_rating.lower()}) increase waiting between shots, which can affect your rhythm and enjoyment of the round")
    elif busyness_rating == "Quiet":
        bullets.append("Quieter conditions today mean less waiting between shots, allowing you to maintain a good rhythm and enjoy a more relaxed pace")
    elif busyness_rating == "Moderate":
        bullets.append("Moderate course busyness today means some waiting is likely, but it shouldn't significantly disrupt your round rhythm")
    
    # Handicap suitability explanations - explain WHAT and WHY
    if handicap_suitability == "Not ideal today":
        # Reference specific conditions and explain impact
        if busyness_rating in ["Very busy", "Busy"]:
            bullets.append(f"Given your handicap of {handicap}, today's busy conditions combined with course difficulty are likely to add unnecessary pressure and slow your pace")
        elif not is_weather_favorable(weather_rating):
            if weather_rating == "Rainy":
                bullets.append(f"Given your handicap of {handicap}, today's rainy conditions combined with course difficulty will make the round more challenging than necessary")
            elif weather_rating == "Windy":
                bullets.append(f"Given your handicap of {handicap}, today's windy conditions combined with course difficulty will make the round more challenging than necessary")
            elif weather_rating == "Cold":
                bullets.append(f"Given your handicap of {handicap}, today's cold conditions combined with course difficulty will make the round more challenging than necessary")
            else:
                bullets.append(f"Given your handicap of {handicap}, today's poor weather conditions combined with course difficulty will make the round more challenging than necessary")
        elif ground_condition in ["Soft", "Soggy"]:
            bullets.append(f"Given your handicap of {handicap}, today's soft ground conditions combined with course difficulty will make recovery shots and approach play harder")
        else:
            bullets.append(f"Given your handicap of {handicap}, today's course difficulty level is likely to add unnecessary difficulty to your round")
    elif handicap_suitability == "Well suited":
        bullets.append(f"Given your handicap of {handicap}, today's course conditions are well matched to your skill level, allowing you to play your natural game")
    
    # Ensure we have exactly 3 bullets
    # Prioritise: daylight, weather, then others
    priority_bullets = []
    seen_types = set()
    
    # First pass: add daylight if present
    for bullet in bullets:
        if "daylight" in bullet.lower() or "light" in bullet.lower():
            priority_bullets.append(bullet)
            seen_types.add("daylight")
            break
    
    # Second pass: add weather if present
    for bullet in bullets:
        if "weather" in bullet.lower() and "weather" not in seen_types:
            priority_bullets.append(bullet)
            seen_types.add("weather")
            break
    
    # Third pass: add ground if present
    for bullet in bullets:
        if ("ground" in bullet.lower() or "wet" in bullet.lower()) and "ground" not in seen_types:
            priority_bullets.append(bullet)
            seen_types.add("ground")
            break
    
    # Fourth pass: add busyness if present
    for bullet in bullets:
        if ("busy" in bullet.lower() or "quiet" in bullet.lower()) and "busyness" not in seen_types:
            priority_bullets.append(bullet)
            seen_types.add("busyness")
            break
    
    # Fifth pass: add handicap if present
    for bullet in bullets:
        if ("handicap" in bullet.lower() or "difficulty" in bullet.lower()) and "handicap" not in seen_types:
            priority_bullets.append(bullet)
            seen_types.add("handicap")
            break
    
    # Fill remaining slots with any remaining bullets
    for bullet in bullets:
        if bullet not in priority_bullets and len(priority_bullets) < 3:
            priority_bullets.append(bullet)
    
    # Ensure exactly 3 bullets with explanatory content
    while len(priority_bullets) < 3:
        # Use available data to create explanatory fallback messages
        if play_recommendation == "Play":
            # Reference the best available condition with explanation
            if is_weather_favorable(weather_rating) and "weather" not in seen_types:
                priority_bullets.append("Dry weather conditions today provide ideal ball flight and comfortable playing conditions")
                seen_types.add("weather")
            elif ground_condition in ["Firm", "Mixed"] and "ground" not in seen_types:
                if ground_condition == "Firm":
                    priority_bullets.append("Firm ground conditions provide good ball roll and predictable bounce, making approach shots easier")
                else:
                    priority_bullets.append("Mixed ground conditions mean some areas will be firmer than others, requiring adaptation throughout the round")
                seen_types.add("ground")
            elif busyness_rating in ["Quiet", "Moderate"] and "busyness" not in seen_types:
                if busyness_rating == "Quiet":
                    priority_bullets.append("Quieter conditions today mean less waiting between shots, allowing you to maintain a good rhythm")
                else:
                    priority_bullets.append("Moderate course busyness today means some waiting is likely, but it shouldn't significantly disrupt your round")
                seen_types.add("busyness")
            else:
                # Combine available conditions
                if "weather" not in seen_types and "ground" not in seen_types:
                    is_challenging = not is_weather_favorable(weather_rating) or ground_condition in ['Soft', 'Soggy']
                    priority_bullets.append(f"{weather_rating.lower().capitalize()} weather and {ground_condition.lower()} ground conditions together create {('challenging' if is_challenging else 'favourable')} playing conditions")
                    seen_types.add("weather")
                    seen_types.add("ground")
                else:
                    break
        else:
            # Reference the worst available condition with explanation
            if not is_weather_favorable(weather_rating) and "weather" not in seen_types:
                if weather_rating == "Rainy":
                    priority_bullets.append("Rainy conditions today will affect ball flight, visibility, and overall comfort during your round")
                elif weather_rating == "Windy":
                    priority_bullets.append("Windy conditions today will significantly affect ball flight and distance control")
                elif weather_rating == "Cold":
                    priority_bullets.append("Cold conditions today will affect ball flight distance and make it harder to maintain flexibility")
                else:
                    priority_bullets.append("Poor weather conditions today will affect ball flight, visibility, and overall comfort during your round")
                seen_types.add("weather")
            elif ground_condition in ["Soft", "Soggy"] and "ground" not in seen_types:
                if ground_condition == "Soggy":
                    priority_bullets.append("Recent rain has left the ground very wet, which makes longer approaches and recovery shots harder")
                else:
                    priority_bullets.append("Soft ground conditions from recent rain make approach shots and recovery shots more difficult")
                seen_types.add("ground")
            elif busyness_rating in ["Very busy", "Busy"] and "busyness" not in seen_types:
                priority_bullets.append(f"Busier tee times today ({busyness_rating.lower()}) increase waiting between shots, which can affect your rhythm and enjoyment")
                seen_types.add("busyness")
            else:
                # Combine available conditions
                if "weather" not in seen_types and "ground" not in seen_types:
                    priority_bullets.append(f"{weather_rating.lower().capitalize()} weather and {ground_condition.lower()} ground conditions together create challenging playing conditions")
                    seen_types.add("weather")
                    seen_types.add("ground")
                else:
                    break
        
        # Prevent infinite loop
        if len(priority_bullets) >= 3:
            break
    
    return priority_bullets[:3]


def generate_what_to_do(play_recommendation, weather_rating, busyness_rating, handicap_suitability, daylight_label, recommended_holes, time_of_day, day, handicap, ground_condition):
    """
    Generate practical advice section.
    If Play: "What to expect"
    If Don't play: "What to do instead"
    Each recommendation is a separate sentence ending with a full stop.
    Each recommendation explains WHY it helps someone with the user's handicap.
    """
    if play_recommendation == "Play":
        advice_parts = []
        
        if recommended_holes == 9:
            advice_parts.append(f"Plan for {recommended_holes} holes to ensure you finish in daylight.")
        else:
            advice_parts.append(f"{recommended_holes} holes should be manageable.")
        
        if busyness_rating in ["Quiet", "Moderate"]:
            advice_parts.append(f"Course busyness is {busyness_rating.lower()}, so you should have plenty of space on the course without long waits between shots.")
        elif busyness_rating in ["Busy", "Very busy"]:
            advice_parts.append(f"Course busyness is {busyness_rating.lower()}, so be prepared for slower play and longer waits between shots.")
        
        if is_weather_favorable(weather_rating):
            advice_parts.append(f"Weather is {weather_rating.lower()}, which provides predictable ball flight and comfortable playing conditions.")
        
        return " ".join(advice_parts) if advice_parts else f"With {recommended_holes} holes planned and current conditions, you should be able to complete your round."
    else:
        # Don't play - what to do instead
        # Each recommendation must be a separate sentence ending with a full stop
        # Each must explain WHY it helps someone with the user's handicap
        alternatives = []
        
        # Determine handicap category for more specific advice
        if handicap <= 9:
            handicap_category = "lower"
        elif handicap <= 19:
            handicap_category = "mid"
        elif handicap <= 28:
            handicap_category = "higher"
        else:
            handicap_category = "higher"
        
        if daylight_label == "Not feasible":
            alternatives.append(f"Try booking an earlier tee time tomorrow. With your handicap of {handicap}, finishing in daylight is important to avoid rushed shots and maintain proper form.")
        elif daylight_label == "Tight":
            alternatives.append(f"Consider playing {recommended_holes} holes instead, or start earlier. With your handicap of {handicap}, tight daylight adds pressure that can affect your swing tempo.")
        
        if not is_weather_favorable(weather_rating):
            if day == "Today":
                if weather_rating == "Rainy":
                    alternatives.append(f"Check tomorrow's forecast for better conditions. Rainy weather affects ball flight more significantly for players with your handicap of {handicap}, making it harder to judge distances and control shots.")
                elif weather_rating == "Windy":
                    alternatives.append(f"Check tomorrow's forecast for better conditions. Windy conditions affect ball flight more significantly for players with your handicap of {handicap}, making it harder to judge distances and control shots.")
                elif weather_rating == "Cold":
                    alternatives.append(f"Check tomorrow's forecast for better conditions. Cold conditions affect ball flight distance and flexibility more significantly for players with your handicap of {handicap}, making it harder to maintain consistent swing tempo.")
                else:
                    alternatives.append(f"Check tomorrow's forecast for better conditions. Poor weather conditions affect ball flight more significantly for players with your handicap of {handicap}, making it harder to judge distances and control shots.")
            else:
                if weather_rating == "Rainy":
                    alternatives.append(f"Wait for a day with better weather. Rainy conditions penalise players with your handicap of {handicap} more heavily, as they affect ball flight and distance control.")
                elif weather_rating == "Windy":
                    alternatives.append(f"Wait for a day with better weather. Windy conditions penalise players with your handicap of {handicap} more heavily, as they affect ball flight and distance control.")
                elif weather_rating == "Cold":
                    alternatives.append(f"Wait for a day with better weather. Cold conditions penalise players with your handicap of {handicap} more heavily, as they affect ball flight distance and swing flexibility.")
                else:
                    alternatives.append(f"Wait for a day with better weather. Poor weather conditions penalise players with your handicap of {handicap} more heavily, as they affect ball flight and distance control.")
        
        if ground_condition in ["Soft", "Soggy"]:
            alternatives.append(f"Consider waiting for firmer ground conditions. Softer ground penalises players with your handicap of {handicap} more heavily, as approach shots won't roll or bounce as expected, making distance control harder.")
        
        if busyness_rating in ["Busy", "Very busy"]:
            alternatives.append(f"Try booking at a quieter time, like early morning or late afternoon. Quieter periods reduce waiting and help players with your handicap of {handicap} maintain tempo and rhythm between shots.")
        
        if handicap_suitability == "Not ideal today":
            if ground_condition in ["Soft", "Soggy"]:
                alternatives.append(f"Consider trying a less demanding course today. Softer ground and tighter layouts penalise players with your handicap of {handicap} more heavily, as they require more precise shot placement.")
            elif busyness_rating in ["Busy", "Very busy"]:
                alternatives.append(f"Consider trying a less demanding course today. Busier conditions combined with course difficulty add unnecessary pressure for players with your handicap of {handicap}, affecting rhythm and enjoyment.")
            else:
                alternatives.append(f"Consider trying a less demanding course today. Course difficulty combined with today's conditions adds unnecessary challenge for players with your handicap of {handicap}.")
        
        if not alternatives:
            alternatives.append(f"Try again tomorrow or choose a different time slot. Today's conditions are likely to add unnecessary difficulty for players with your handicap of {handicap}.")
        
        # Return up to 3 recommendations, each as a separate sentence
        return " ".join(alternatives[:3]) if len(alternatives) >= 3 else " ".join(alternatives) if alternatives else f"Consider trying again another day. Today's conditions are likely to add unnecessary difficulty for players with your handicap of {handicap}."


def get_price_label(price_tier: str) -> str:
    """
    Convert price tier symbol to descriptive label with range.
    Returns: "Low (£0–40)", "Mid (£40–70)", "High (£70+)", or "Unknown"
    """
    if price_tier == "£":
        return "Low (£0–40)"
    elif price_tier == "££":
        return "Mid (£40–70)"
    elif price_tier == "£££":
        return "High (£70+)"
    else:
        return "Unknown"


def get_weather_label(weather_rating: str, weather_data: dict = None) -> str:
    """
    Return weather rating as-is (already in condition-based format).
    Returns: "Dry", "Rainy", "Windy", "Cold", or "Poor conditions"
    """
    # Weather rating is already in condition-based format from calculate_weather_rating
    return weather_rating


def normalize_weather_label_for_display(weather_label: str, weather_data: dict = None) -> str:
    """
    Normalize weather label to user-friendly display values.
    Returns ONE of: "Dry", "Light rain", "Rain", "Windy", "Cold", "Frost risk"
    Chooses the most impactful condition based on available data.
    Prioritizes: precipitation > wind > frost risk > cold > dry
    Does not invent conditions if not present.
    """
    if not weather_label:
        return "Dry"
    
    # If we have weather_data, use it to determine most impactful condition
    if weather_data:
        precipitation = weather_data.get("precipitation", 0)
        wind_speed = weather_data.get("wind_speed", 0)
        temp_min = weather_data.get("temperature_min", 10)
        temp_max = weather_data.get("temperature_max", 10)
        temp_avg = (temp_min + temp_max) / 2
        
        # Prioritize: precipitation > wind > frost risk > cold > dry
        if precipitation >= 5:
            return "Rain"
        elif precipitation >= 1:
            return "Light rain"
        elif wind_speed >= 30:
            return "Windy"
        elif temp_min < 2:  # Frost risk threshold (check min temp, not avg)
            return "Frost risk"
        elif temp_avg < 5:
            return "Cold"
        else:
            return "Dry"
    
    # Fallback: map internal labels to display labels
    weather_label_lower = weather_label.lower()
    
    # Map precipitation labels
    if weather_label_lower in ["rain", "heavy rain"]:
        return "Rain"
    elif weather_label_lower in ["showers", "light rain"]:
        return "Light rain"
    
    # Map wind labels
    if weather_label_lower in ["windy", "very windy", "breezy"]:
        return "Windy"
    
    # Map temperature labels (but don't invent frost risk without data)
    if weather_label_lower in ["very cold", "freezing"]:
        return "Cold"
    elif weather_label_lower == "cold":
        return "Cold"
    
    # Default to Dry
    if weather_label_lower == "dry":
        return "Dry"
    
    # Final fallback
    return "Dry"


def normalize_suitability_label_for_display(suitability_label: str, handicap: int) -> str:
    """
    Normalize suitability label to user-friendly display values.
    Returns ONE of:
    - "Good for a {handicap} handicap"
    - "Tough for a {handicap} handicap today"
    Never returns "Borderline" or "Not ideal".
    """
    if not suitability_label:
        return f"Tough for a {handicap} handicap today"
    
    suitability_lower = suitability_label.lower()
    
    # If already in correct format, return as-is (but ensure handicap matches)
    if f"good for a {handicap} handicap" in suitability_lower or "good for a" in suitability_lower:
        return f"Good for a {handicap} handicap"
    if f"tough for a {handicap} handicap today" in suitability_lower or "tough for a" in suitability_lower:
        return f"Tough for a {handicap} handicap today"
    
    # Check for positive indicators
    if "well suited" in suitability_lower or "good" in suitability_lower or "suits" in suitability_lower:
        return f"Good for a {handicap} handicap"
    
    # Everything else is "Tough"
    return f"Tough for a {handicap} handicap today"


def generate_banner_summary(reasons, verdict, handicap, weather_label_display, ground_label_display, busyness_label, suitability_label_display):
    """
    Generate a short, specific summary sentence for the banner explaining the decision.
    Uses top drivers (weather/ground/busyness/handicap fit).
    Format: "[Factor conditions] will [impact] at a {handicap} handicap."
    Example: "Cold air and soft ground will cost distance and make recovery shots harder at a 25 handicap."
    """
    if not reasons:
        if verdict == "Play":
            return f"Good conditions for a {handicap} handicap today."
        else:
            return f"Conditions add challenge for a {handicap} handicap today."
    
    # Prioritize factors: weather, ground, busyness, suitability
    factor_priority = ["weather", "ground", "busyness", "suitability"]
    selected_factors = []
    
    for factor in factor_priority:
        for reason in reasons:
            if reason.get("factor") == factor:
                selected_factors.append(reason)
                break
        if len(selected_factors) >= 2:  # Use top 2 factors
            break
    
    # Build summary sentence from selected factors
    condition_parts = []
    impact_parts = []
    
    # Weather part
    weather_reason = next((r for r in selected_factors if r.get("factor") == "weather"), None)
    if weather_reason:
        weather_desc = weather_label_display.lower()
        if weather_desc in ["rain", "light rain"]:
            condition_parts.append(weather_desc)
            impact_parts.append("affect ball flight")
        elif weather_desc == "windy":
            condition_parts.append("wind")
            impact_parts.append("affect ball flight")
        elif weather_desc == "cold":
            condition_parts.append("cold air")
            impact_parts.append("cost distance")
        elif weather_desc == "frost risk":
            condition_parts.append("frost risk")
            impact_parts.append("affect play")
    
    # Ground part
    ground_reason = next((r for r in selected_factors if r.get("factor") == "ground"), None)
    if ground_reason:
        ground_desc = ground_label_display.lower()
        if "soft" in ground_desc or "too soft" in ground_desc:
            condition_parts.append("soft ground")
            impact_parts.append("make recovery shots harder")
        elif "firm" in ground_desc:
            condition_parts.append("firm ground")
            impact_parts.append("provide good roll")
    
    # Busyness part (only if significant and not ideal)
    if verdict != "Play":
        busyness_reason = next((r for r in selected_factors if r.get("factor") == "busyness"), None)
        if busyness_reason and busyness_label.lower() in ["busy", "very busy"]:
            condition_parts.append(f"{busyness_label.lower()} conditions")
            impact_parts.append("slow pace")
    
    # Suitability part (only if not ideal)
    if verdict != "Play":
        suitability_reason = next((r for r in selected_factors if r.get("factor") == "suitability"), None)
        if suitability_reason and "tough" in suitability_label_display.lower():
            # Already captured in ground/weather, but can add if needed
            pass
    
    # Combine into sentence
    if condition_parts and impact_parts:
        # Use first 2 conditions and their impacts
        conditions = " and ".join(condition_parts[:2])
        impacts = " and ".join(impact_parts[:2])
        return f"{conditions.capitalize()} will {impacts} at a {handicap} handicap."
    
    # Fallback
    if verdict == "Play":
        return f"Good conditions for a {handicap} handicap today."
    else:
        return f"Conditions add challenge for a {handicap} handicap today."


def generate_handicap_aware_why_bullets(reasons, handicap, weather_label, ground_label, busyness_label):
    """
    Generate 3-4 handicap-aware why bullets.
    Format: "[Factor]: [what's happening], so at a {handicap} handicap you can expect [impact]."
    
    Handicap rules:
    - Low (0-12): Downweight course difficulty and busyness, focus on safety/comfort (weather, extreme conditions)
    - Mid (13-24): Balanced explanation across all factors
    - High (25-54): Emphasize reduced forgiveness (less roll, heavier lies, harder recovery, slower pace adds pressure)
    """
    # Determine handicap band
    if handicap <= 12:
        handicap_band = "low"
    elif handicap <= 24:
        handicap_band = "mid"
    else:
        handicap_band = "high"
    
    bullets = []
    seen_factors = set()
    
    # Factor priority based on handicap band
    if handicap_band == "low":
        # Low handicap: prioritize weather (safety/comfort), then ground, downweight busyness/difficulty
        factor_priority = ["weather", "ground", "busyness", "suitability"]
    elif handicap_band == "mid":
        # Mid handicap: balanced
        factor_priority = ["weather", "ground", "busyness", "suitability"]
    else:  # high
        # High handicap: emphasize all factors, especially ground and busyness (forgiveness)
        factor_priority = ["ground", "weather", "busyness", "suitability"]
    
    # Map factor names to display names
    factor_display_names = {
        "weather": "Weather",
        "ground": "Ground",
        "busyness": "Course pressure",
        "suitability": "Course difficulty"
    }
    
    # Process reasons in priority order
    for factor in factor_priority:
        if len(bullets) >= 4:
            break
        
        # Find reason for this factor
        reason = next((r for r in reasons if r.get("factor") == factor), None)
        if not reason or factor in seen_factors:
            continue
        
        seen_factors.add(factor)
        condition = reason.get("condition", "")
        impact = reason.get("impact", "")
        
        # Generate handicap-specific bullet
        factor_display = factor_display_names.get(factor, factor.capitalize())
        
        if factor == "weather":
            # Weather: focus on safety/comfort for low, impact on playability for mid/high
            if handicap_band == "low":
                if weather_label in ["Rain", "Light rain"]:
                    what_happening = f"{weather_label.lower()} reduces visibility and makes footing slippery"
                    impact_text = "less control over ball flight and increased risk of slips"
                elif weather_label == "Windy":
                    what_happening = "strong winds affect ball flight"
                    impact_text = "more challenging shot control and course management"
                elif weather_label == "Cold":
                    what_happening = "cold temperatures reduce ball distance"
                    impact_text = "clubs playing shorter than expected and less comfortable conditions"
                elif weather_label == "Frost risk":
                    what_happening = "frost risk may delay start times"
                    impact_text = "potential delays and colder playing conditions"
                else:  # Dry
                    what_happening = "dry conditions provide clear visibility"
                    impact_text = "optimal ball flight and comfortable playing conditions"
            elif handicap_band == "mid":
                if weather_label in ["Rain", "Light rain"]:
                    what_happening = f"{weather_label.lower()} affects ball flight and visibility"
                    impact_text = "more challenging shot control and course management"
                elif weather_label == "Windy":
                    what_happening = "wind affects ball flight"
                    impact_text = "more challenging shot control and distance judgment"
                elif weather_label == "Cold":
                    what_happening = "cold air reduces ball distance"
                    impact_text = "clubs playing shorter and less roll on approach shots"
                else:
                    what_happening = "dry conditions provide good visibility"
                    impact_text = "predictable ball flight and good course management"
            else:  # high
                if weather_label in ["Rain", "Light rain"]:
                    what_happening = f"{weather_label.lower()} reduces ball distance and makes lies heavier"
                    impact_text = "less forgiveness on mis-hits and harder recovery shots"
                elif weather_label == "Windy":
                    what_happening = "wind amplifies ball flight errors"
                    impact_text = "mis-hits travel further offline and distance control becomes harder"
                elif weather_label == "Cold":
                    what_happening = "cold air significantly reduces distance"
                    impact_text = "less roll on approach shots and recovery shots require more club"
                else:
                    what_happening = "dry conditions provide good ball flight"
                    impact_text = "more predictable distance and better roll on approach shots"
        
        elif factor == "ground":
            # Ground: emphasize forgiveness impact for high handicap
            if handicap_band == "low":
                if "soft" in ground_label.lower() or "too soft" in ground_label.lower():
                    what_happening = f"{ground_label.lower()} conditions reduce ball roll"
                    impact_text = "less predictable approach shots and potentially plugged lies"
                elif ground_label == "Firm":
                    what_happening = "firm ground provides fast roll"
                    impact_text = "predictable approach shots and clean lies"
                else:
                    what_happening = "normal ground conditions"
                    impact_text = "standard ball roll and predictable lies"
            elif handicap_band == "mid":
                if "soft" in ground_label.lower() or "too soft" in ground_label.lower():
                    what_happening = f"{ground_label.lower()} conditions reduce ball roll"
                    impact_text = "less predictable approach shots and harder recovery shots"
                elif ground_label == "Firm":
                    what_happening = "firm ground provides good roll"
                    impact_text = "predictable approach shots and clean lies"
                else:
                    what_happening = "normal ground conditions"
                    impact_text = "standard ball roll and predictable lies"
            else:  # high
                if "soft" in ground_label.lower() or "too soft" in ground_label.lower():
                    what_happening = f"{ground_label.lower()} conditions significantly reduce roll"
                    impact_text = "less forgiveness on approach shots, heavier lies make recovery harder, and plugged balls reduce distance"
                elif ground_label == "Firm":
                    what_happening = "firm ground provides good roll"
                    impact_text = "more forgiveness on approach shots and cleaner lies for recovery"
                else:
                    what_happening = "normal ground conditions"
                    impact_text = "standard roll and manageable lies for recovery shots"
        
        elif factor == "busyness":
            # Busyness: downweight for low, emphasize pressure for high
            if handicap_band == "low":
                if busyness_label in ["Busy", "Very busy"]:
                    what_happening = f"{busyness_label.lower()} conditions mean longer waits"
                    impact_text = "slower pace of play and potential delays"
                else:
                    what_happening = f"{busyness_label.lower()} conditions"
                    impact_text = "good pace of play"
            elif handicap_band == "mid":
                if busyness_label in ["Busy", "Very busy"]:
                    what_happening = f"{busyness_label.lower()} conditions mean longer waits between shots"
                    impact_text = "slower pace of play and potential rhythm disruption"
                else:
                    what_happening = f"{busyness_label.lower()} conditions"
                    impact_text = "good pace of play"
            else:  # high
                if busyness_label in ["Busy", "Very busy"]:
                    what_happening = f"{busyness_label.lower()} conditions mean longer waits and more pressure"
                    impact_text = "slower pace adds pressure on each shot, making recovery harder when you're waiting between shots"
                else:
                    what_happening = f"{busyness_label.lower()} conditions"
                    impact_text = "good pace allows time to recover between shots"
        
        elif factor == "suitability":
            # Suitability: downweight for low, emphasize challenge for high
            if handicap_band == "low":
                # For low handicap, only mention if it's significantly challenging
                if "tough" in condition.lower() or "challenging" in condition.lower():
                    what_happening = f"course difficulty is {condition.lower()}"
                    impact_text = "more demanding course management and shot selection"
                else:
                    # Skip for low handicap if not challenging
                    continue
            elif handicap_band == "mid":
                if "tough" in condition.lower() or "challenging" in condition.lower():
                    what_happening = f"course difficulty is {condition.lower()}"
                    impact_text = "more challenging course management and shot selection"
                else:
                    what_happening = f"course difficulty is {condition.lower()}"
                    impact_text = "manageable course layout and shot selection"
            else:  # high
                if "tough" in condition.lower() or "challenging" in condition.lower():
                    what_happening = f"course difficulty is {condition.lower()}"
                    impact_text = "less forgiveness on mis-hits, tighter landing areas, and harder recovery shots"
                else:
                    what_happening = f"course difficulty is {condition.lower()}"
                    impact_text = "more forgiving course layout with wider landing areas"
        
        # Format bullet: "[Factor]: [what's happening], so at a {handicap} handicap you can expect [impact]."
        bullet = f"{factor_display}: {what_happening}, so at a {handicap} handicap you can expect {impact_text}."
        bullets.append(bullet)
    
    # Ensure we have 3-4 bullets by filling from remaining reasons if needed
    if len(bullets) < 3:
        for reason in reasons:
            if len(bullets) >= 4:
                break
            factor = reason.get("factor", "")
            if factor in seen_factors:
                continue
            
            seen_factors.add(factor)
            condition = reason.get("condition", "")
            factor_display = factor_display_names.get(factor, factor.capitalize())
            
            # Generate generic bullet if we don't have enough
            if factor == "weather":
                what_happening = f"{weather_label.lower()} conditions"
                if handicap_band == "high":
                    impact_text = "less predictable ball flight and distance control"
                else:
                    impact_text = "affects ball flight and course management"
            elif factor == "ground":
                what_happening = f"{ground_label.lower()} conditions"
                if handicap_band == "high":
                    impact_text = "less roll and heavier lies make recovery shots harder"
                else:
                    impact_text = "affects ball roll and approach shots"
            elif factor == "busyness":
                what_happening = f"{busyness_label.lower()} conditions"
                if handicap_band == "high":
                    impact_text = "slower pace adds pressure on each shot"
                else:
                    impact_text = "affects pace of play"
            elif factor == "suitability":
                what_happening = f"course difficulty is {condition.lower()}"
                if handicap_band == "high":
                    impact_text = "less forgiveness on mis-hits and harder recovery"
                else:
                    impact_text = "affects course management"
            else:
                continue
            
            bullet = f"{factor_display}: {what_happening}, so at a {handicap} handicap you can expect {impact_text}."
            bullets.append(bullet)
    
    # Return exactly 3-4 bullets
    return bullets[:4] if len(bullets) >= 3 else bullets


def get_ground_label(ground_info) -> str:
    """
    Extract ground label from ground_info dict or return as-is if already a string.
    Returns: "Firm", "Normal", "Soft (Playable but heavy)", or "Too soft (Likely to affect play)"
    """
    if isinstance(ground_info, dict):
        label = ground_info.get("ground_label", "Normal")
    else:
        # Backward compatibility: if it's a string, return it
        label = ground_info
    
    # Format labels with descriptions
    if label == "Firm":
        return "Firm (fast, clean lies)"
    elif label == "Normal":
        return "Normal"
    elif label == "Soft":
        return "Soft (playable but heavy)"
    elif label == "Too soft":
        return "Too soft (likely to affect play)"
    else:
        return label


def get_suitability_label(handicap_suitability: str) -> str:
    """
    Convert handicap suitability to explicit, self-explanatory label.
    Returns: "Well suited" or "Challenging for your handicap today" (binary decision)
    """
    if handicap_suitability == "Well suited":
        return "Well suited"
    elif handicap_suitability == "Not ideal today":
        return "Challenging for your handicap today"
    else:
        return handicap_suitability


def generate_explanation_deterministic(weather_rating, ground_condition, busyness_rating, handicap_suitability, price_label, tomorrow_forecast, recommended_holes=None):
    """
    Generate deterministic explanation paragraph summarising all ratings.
    """
    parts = []
    
    parts.append(f"Weather today is {weather_rating.lower()}")
    if tomorrow_forecast:
        parts.append(f"with tomorrow's forecast {tomorrow_forecast.lower()}")
    parts.append(f"and ground condition is {ground_condition.lower()} based on recent rainfall.")
    
    parts.append(f"Course busyness is estimated as {busyness_rating.lower()} (not live tee times)")
    parts.append(f"based on seasonality, weather attractiveness, day of week, time of day, and course popularity.")
    
    parts.append(f"For your handicap, this course is {handicap_suitability.lower()} today.")
    
    if recommended_holes:
        if recommended_holes == 18:
            parts.append(f"{recommended_holes} holes looks feasible before sunset.")
        else:
            parts.append(f"{recommended_holes} holes is the safer call for daylight.")
    
    parts.append(f"Price tier is {price_label.lower()} (typical estimate based on course type).")
    
    return " ".join(parts)


async def rewrite_reasons_and_recommendations_llm(deterministic_data, request_id: str = None):
    """
    Rewrite deterministic reasons and recommendations using LLM for clarity.
    LLM is NOT allowed to invent new reasons - only rewrite existing ones.
    
    deterministic_data: dict containing:
        - reasons: list of reason dicts with "factor", "condition", "threshold", "impact"
        - recommendations: list of recommendation dicts with "action", "reason"
        - verdict: str (Play / Not ideal) - binary decision only
        - course_name: str
        - handicap: int
    
    Returns: dict with same structure, rewritten text only
    Raises exception if API call fails or returns invalid structure.
    """
    import json
    
    # Read OPENAI_API_KEY from environment
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY missing")
    
    # Import OpenAI
    try:
        from openai import OpenAI
    except ImportError:
        raise ValueError("OpenAI package not installed")
    
    client = OpenAI(api_key=api_key)
    
    # Extract deterministic data
    reasons = deterministic_data["reasons"]
    recommendations = deterministic_data["recommendations"]
    verdict = deterministic_data.get("verdict", "Play")
    course_name = deterministic_data.get("course_name", "")
    handicap = deterministic_data.get("handicap", 0)
    
    # Prepare structured input
    structured_input = {
        "reasons": reasons,
        "recommendations": recommendations,
        "verdict": verdict,
        "course_name": course_name,
        "handicap": handicap
    }
    
    prompt = f"""You are rewriting golf course assessment text for clarity and simplicity. You MUST follow these rules:

CRITICAL RULES:
1. DO NOT invent new reasons or recommendations
2. DO NOT change the meaning or structure
3. DO NOT add new factors or conditions
4. DO NOT remove any reasons or recommendations
5. ONLY rewrite the text for clarity and simplicity
6. Keep the same number of reasons and recommendations
7. Keep the same factor references (weather, ground, busyness, suitability)

INPUT DATA (deterministic output):
{json.dumps(structured_input, indent=2)}

TASK:
Rewrite the "impact" text in each reason and the "reason" text in each recommendation for clarity and simplicity. Keep British English. Use simple, clear language.

REQUIRED OUTPUT FORMAT (JSON):
{{
    "reasons": [
        {{
            "factor": "weather",
            "condition": "Rain",
            "threshold": "Rain (25)",
            "impact": "[rewritten impact text - same meaning, clearer wording]"
        }},
        ...
    ],
    "recommendations": [
        {{
            "action": "[keep original action]",
            "reason": "[rewritten reason text - same meaning, clearer wording]"
        }},
        ...
    ]
}}

VALIDATION:
- Must return exactly the same number of reasons
- Must return exactly the same number of recommendations
- Each reason must have the same "factor", "condition", "threshold" keys
- Each recommendation must have the same "action" key
- Only rewrite the "impact" and "reason" text fields
- Do not change any other fields

Return ONLY valid JSON matching the structure above."""
    
    request_id_str = f" request_id={request_id}" if request_id else ""
    logger.info(f"Calling OpenAI to rewrite reasons/recommendations{request_id_str}")
    
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.chat.completions.create,
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a text editor rewriting golf course assessment text for clarity. You must preserve all meaning and structure. Return only valid JSON matching the exact structure provided. Do not invent new content."
                    },
                    {"role": "user", "content": prompt}
                ],
                max_tokens=800,
                temperature=0.2,
                response_format={"type": "json_object"}
            ),
            timeout=10.0
        )
        
        response_text = response.choices[0].message.content.strip()
        
        # Parse JSON response
        try:
            rewritten_data = json.loads(response_text)
        except json.JSONDecodeError as e:
            logger.error(f"LLM returned invalid JSON: {e}")
            raise ValueError("LLM returned invalid JSON structure")
        
        # Validate structure
        if "reasons" not in rewritten_data or "recommendations" not in rewritten_data:
            logger.error("LLM response missing required keys")
            raise ValueError("LLM response missing required keys")
        
        rewritten_reasons = rewritten_data["reasons"]
        rewritten_recommendations = rewritten_data["recommendations"]
        
        # Validate same count
        if len(rewritten_reasons) != len(reasons):
            logger.error(f"LLM changed reason count: {len(reasons)} -> {len(rewritten_reasons)}")
            raise ValueError("LLM changed number of reasons")
        
        if len(rewritten_recommendations) != len(recommendations):
            logger.error(f"LLM changed recommendation count: {len(recommendations)} -> {len(rewritten_recommendations)}")
            raise ValueError("LLM changed number of recommendations")
        
        # Validate structure of each reason
        for i, (original, rewritten) in enumerate(zip(reasons, rewritten_reasons)):
            if not isinstance(rewritten, dict):
                raise ValueError(f"Reason {i} is not a dict")
            if "factor" not in rewritten or "impact" not in rewritten:
                raise ValueError(f"Reason {i} missing required fields")
            # Ensure factor matches (LLM shouldn't change this)
            if rewritten.get("factor") != original.get("factor"):
                logger.warning(f"LLM changed factor in reason {i}, keeping original")
                rewritten["factor"] = original.get("factor")
            if "condition" in original:
                rewritten["condition"] = original.get("condition")
            if "threshold" in original:
                rewritten["threshold"] = original.get("threshold")
        
        # Validate structure of each recommendation
        for i, (original, rewritten) in enumerate(zip(recommendations, rewritten_recommendations)):
            if not isinstance(rewritten, dict):
                raise ValueError(f"Recommendation {i} is not a dict")
            if "action" not in rewritten or "reason" not in rewritten:
                raise ValueError(f"Recommendation {i} missing required fields")
            # Ensure action matches (LLM shouldn't change this)
            if rewritten.get("action") != original.get("action"):
                logger.warning(f"LLM changed action in recommendation {i}, keeping original")
                rewritten["action"] = original.get("action")
        
        # Quality validation: check if rewritten text is reasonable
        for i, (original, rewritten) in enumerate(zip(reasons, rewritten_reasons)):
            original_impact = original.get("impact", "")
            rewritten_impact = rewritten.get("impact", "")
            
            # Check if rewritten text is too short (less than 20 chars) or empty
            if len(rewritten_impact.strip()) < 20:
                logger.warning(f"Rewritten reason {i} is too short, using original")
                rewritten["impact"] = original_impact
                continue
            
            # Check if rewritten text is suspiciously similar to original (might indicate no rewrite)
            # But allow some similarity since we're rewriting for clarity, not completely changing
            
        for i, (original, rewritten) in enumerate(zip(recommendations, rewritten_recommendations)):
            original_reason = original.get("reason", "")
            rewritten_reason = rewritten.get("reason", "")
            
            # Check if rewritten text is too short (less than 20 chars) or empty
            if len(rewritten_reason.strip()) < 20:
                logger.warning(f"Rewritten recommendation {i} is too short, using original")
                rewritten["reason"] = original_reason
                continue
        
        logger.info(f"OpenAI rewrite succeeded{request_id_str}")
        return rewritten_data
        
    except Exception as e:
        logger.error(f"OpenAI rewrite failed: {str(e)}", exc_info=True)
        raise


async def generate_explanation_llm(assessment_data, request_id: str = None):
    """
    Generate explanation using OpenAI LLM with structured input.
    Raises exception if API call fails (to be caught by caller).
    
    assessment_data: dict containing:
        - course_name: str
        - day: str (Today/Tomorrow)
        - time_of_day: str
        - handicap: int
        - weather_rating: str (Good/Mixed/Poor)
        - ground_rating: str (Firm/Mixed/Soft/Soggy)
        - busyness_rating: str (Quiet/Moderate/Busy/Very busy)
        - suitability_rating: str (Well suited/Not ideal today) - binary decision
        - price_label: str (Affordable/Mid-range/Expensive)
        - verdict: str (Play/Don't play)
        - next_action: str
        - recommended_holes: int (18 or 9)
        - daylight_label: str (Plenty of light/Tight/Not feasible)
    request_id: unique request identifier for logging
    """
    # Read OPENAI_API_KEY from environment
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY missing")
    
    # Import OpenAI (synchronous client)
    try:
        from openai import OpenAI
    except ImportError:
        raise ValueError("OpenAI package not installed")
    
    # Instantiate client
    client = OpenAI(api_key=api_key)
    
    # Create structured input as JSON
    structured_input = {
        "course_name": assessment_data["course_name"],
        "day": assessment_data["day"],
        "time_of_day": assessment_data["time_of_day"],
        "handicap": assessment_data["handicap"],
        "weather_rating": assessment_data["weather_rating"],
        "ground_rating": assessment_data["ground_rating"],
        "busyness_rating": assessment_data["busyness_rating"],
        "suitability_rating": assessment_data["suitability_rating"],
        "price_label": assessment_data["price_label"],
        "verdict": assessment_data["verdict"],
        "next_action": assessment_data["next_action"],
        "recommended_holes": assessment_data.get("recommended_holes"),
        "daylight_label": assessment_data.get("daylight_label")
    }
    
    # Extract key values for prompt
    user_time_of_day = assessment_data["time_of_day"]
    user_day = assessment_data["day"]
    verdict = assessment_data["verdict"]
    recommended_holes = assessment_data.get("recommended_holes")
    next_action = assessment_data.get("next_action", "")
    
    # Extract values for short descriptions
    course_name = assessment_data["course_name"]
    weather_rating = assessment_data["weather_rating"]
    ground_rating = assessment_data["ground_rating"]
    busyness_rating = assessment_data["busyness_rating"]
    daylight_label = assessment_data.get("daylight_label", "")
    price_label = assessment_data["price_label"]
    
    # Build daylight short description
    daylight_short = ""
    if recommended_holes:
        if daylight_label == "Plenty of light":
            daylight_short = f"{recommended_holes} holes should work fine"
        elif daylight_label == "Tight":
            daylight_short = f"{recommended_holes} holes is tight but doable"
        elif daylight_label == "Not feasible":
            daylight_short = f"Not enough daylight for {recommended_holes} holes"
        else:
            daylight_short = f"{recommended_holes} holes recommended"
    
    # Build next action for sentence 3
    next_step = ""
    if verdict == "Don't play":
        # Build data-driven next_step based on conditions
        if daylight_label == "Not feasible":
            next_step = "Try booking an earlier tee time tomorrow to ensure you finish in daylight"
        elif recommended_holes == 9:
            next_step = f"Consider {recommended_holes} holes instead, or try morning tomorrow when daylight is better"
        elif not is_weather_favorable(weather_rating):
            if user_day == "Today":
                next_step = f"Check tomorrow's forecast for better {weather_rating.lower()} conditions"
            else:
                next_step = f"Wait for a day with better weather than {weather_rating.lower()}"
        elif busyness_rating in ["Busy", "Very busy"]:
            next_step = f"Try booking at a quieter time, like early morning, when course busyness is typically lower than {busyness_rating.lower()}"
        else:
            if user_day == "Today":
                next_step = "Try morning tomorrow or a quieter course"
            else:
                next_step = "Try morning or a quieter course"
    else:
        # Use next_action if available, otherwise provide data-driven fallback
        next_action_value = assessment_data.get("next_action", "")
        if next_action_value:
            next_step = next_action_value
        else:
            # Fallback: reference the conditions that make it good
            if is_weather_favorable(weather_rating) and busyness_rating in ["Quiet", "Moderate"]:
                next_step = f"With {weather_rating.lower()} weather and {busyness_rating.lower()} course conditions, you should have a good round"
            elif is_weather_favorable(weather_rating):
                next_step = f"With {weather_rating.lower()} weather, conditions are favourable for play"
            else:
                next_step = f"With {recommended_holes} holes planned, you should be able to complete your round"
    
    # Use double braces to escape in f-string
    prompt = f"""Write a short, friendly paragraph following this EXACT structure. Use British English. You may rephrase slightly but must keep it casual and concise.

REQUIRED STRUCTURE:

Sentence 1: "Today at {course_name}, expect {{weather_short}} and {{ground_short}} ground."
- weather_short: Use weather_rating="{weather_rating}" (Good/Mixed/Poor) - rephrase casually (e.g., "good weather", "mixed conditions", "poor conditions")
- ground_short: Use ground_rating="{ground_rating}" (Firm/Mixed/Soft/Soggy) - rephrase casually (e.g., "firm", "mixed", "soft", "soggy")

Sentence 2: "{{busyness_short}}. {{daylight_short}}."
- busyness_short: Use busyness_rating="{busyness_rating}" (Quiet/Moderate/Busy/Very busy) - rephrase casually (e.g., "It's quiet", "Expect moderate crowds", "It'll be busy")
- daylight_short: "{daylight_short}"

Sentence 3: "Verdict: {verdict}. {{next_step}}."
- verdict: Use exactly "{verdict}" (Play or Don't play)
- next_step: "{next_step}"

Optional Sentence 4: "Price: {price_label}."
- price_label: Use exactly "{price_label}" (Affordable, Mid-range, or Expensive)

Tone:
- Sound like a helpful golfer friend, casual and friendly
- Use simple words
- You may rephrase slightly but keep the structure and meaning

Forbidden words:
- Do not use: "rating", "resulting in", "advisable", "hindered", "feasible", "circumstances"
- Do not mention "LLM" or the model
- Do not repeat the same word twice in a row
- Do not use ampersands or em dashes

Assessment data:
{json.dumps(structured_input, indent=2)}

Write the paragraph following the exact structure above."""
    
    # Log immediately before the API call
    request_id_str = f" request_id={request_id}" if request_id else ""
    logger.info(f"Calling OpenAI summary{request_id_str}")
    
    # Make synchronous API call in a thread pool to avoid blocking
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.chat.completions.create,
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a helpful golfer friend giving friendly advice about golf course conditions. Write in a conversational, friendly tone using second person ('you') and simple words. Write 2-4 sentences maximum. Use British English. Sound like a friend, not a report. Do not use formal words like 'rating', 'resulting in', 'advisable', 'hindered', 'feasible', or 'circumstances'. Do not mention LLM or the model. Do not invent facts, numbers, live prices, or live tee times. Do not use ampersands or em dashes."
                    },
                    {"role": "user", "content": prompt}
                ],
                max_tokens=120,
                temperature=0.3
            ),
            timeout=10.0
        )
        
        explanation = response.choices[0].message.content.strip()
        logger.info(f"OpenAI summary succeeded{request_id_str}")
        return explanation
    except Exception as e:
        # Log the exception message before re-raising
        logger.error(f"OpenAI summary failed: {str(e)}", exc_info=True)
        raise


async def generate_explanation(assessment_data, force_llm=False, llm_effective_enabled=False) -> Tuple[str, str]:
    """
    Generate explanation paragraph. Uses LLM if enabled and available, otherwise uses deterministic version.
    
    assessment_data: dict containing all required fields for structured LLM input
    force_llm: parsed llm query parameter (boolean) - when true, forces LLM call regardless of LLM_SUMMARY
    llm_effective_enabled: computed effective LLM enabled status (for badge display)
    
    Returns: tuple of (explanation_text, summary_mode)
        summary_mode: "LLM", "Deterministic (LLM failed)", or "Deterministic"
    """
    # LLM should run if:
    # 1. force_llm is true (from query parameter) AND openai_client exists, OR
    # 2. LLM_SUMMARY_ENABLED is true AND openai_client exists
    # force_llm takes precedence over LLM_SUMMARY_ENABLED
    use_llm = bool(openai_client) and (force_llm or LLM_SUMMARY_ENABLED)
    
    if use_llm:
        # Log that OpenAI call is being executed
        logger.info("LLM: executing OpenAI call")
        
        # Try LLM, fall back silently to deterministic on any error
        try:
            explanation = await generate_explanation_llm(assessment_data)
            return explanation, "LLM"
        except Exception as e:
            # Log the exception message
            logger.error(f"LLM: failed: {str(e)}")
            # Fall back silently to deterministic on any error
            pass
    
    # Use deterministic explanation
    # Convert price_tier to price_label if needed
    price_label_value = assessment_data.get("price_label")
    if not price_label_value:
        price_tier_value = assessment_data.get("price_tier", "££")
        price_label_value = get_price_label(price_tier_value)
    
    deterministic_explanation = generate_explanation_deterministic(
        assessment_data["weather_rating"],
        assessment_data["ground_rating"],
        assessment_data["busyness_rating"],
        assessment_data["suitability_rating"],
        price_label_value,
        None  # tomorrow_forecast not needed for deterministic
    )
    
    # Determine mode: if llm_effective_enabled was true but call failed, show "Deterministic (LLM failed)"
    # Otherwise show "Deterministic"
    if llm_effective_enabled:
        return deterministic_explanation, "Deterministic (LLM failed)"
    else:
        return deterministic_explanation, "Deterministic"


@app.get("/debug/env")
async def debug_env() -> Dict[str, Any]:
    """
    Debug endpoint to check environment variables.
    Returns JSON with OpenAI API key status, LLM_SUMMARY flag value, and effective LLM enabled status.
    """
    # Calculate effective LLM enabled status (matches runtime decision when llm=1 is passed)
    # When llm=1 is passed, LLM runs if OPENAI_API_KEY exists (regardless of LLM_SUMMARY env var)
    llm_effective_enabled = bool(OPENAI_API_KEY)
    
    return {
        "has_openai_key": bool(OPENAI_API_KEY),
        "llm_summary_flag": os.getenv("LLM_SUMMARY", ""),
        "llm_effective_enabled": llm_effective_enabled
    }


@app.get("/debug/version")
async def debug_version() -> Dict[str, Any]:
    """
    Debug endpoint to check version information.
    Returns JSON with git commit hash and build time.
    """
    return {
        "git_commit": GIT_COMMIT,
        "build_time_utc": BUILD_TIME_UTC
    }


@app.get("/courses")
async def get_courses(
    q: str = Query(None, description="Search query for course names"),
    debug: str = Query(None, description="Set to 1 to include debug information")
):
    """
    Search courses by name.
    Returns up to 8 matching courses, sorted by relevance.
    When debug=1, includes debug information in response.
    """
    debug_mode = debug == "1"
    
    # Collect debug information if debug mode is enabled
    debug_info = {}
    if debug_mode:
        debug_info["q_received"] = q if q else None
        debug_info["q_stripped"] = q.strip() if q else None
        debug_info["q_len"] = len(q.strip()) if q else 0
        debug_info["courses_path"] = str(COURSES_PATH)
        debug_info["courses_path_fallback"] = str(COURSES_PATH_FALLBACK)
        debug_info["file_exists"] = COURSES_PATH.exists()
        debug_info["file_exists_fallback"] = COURSES_PATH_FALLBACK.exists()
        
        # Additional debug information
        debug_info["app_cwd"] = str(Path.cwd())
        
        # Check /app directory
        app_dir = Path("/app")
        debug_info["app_dir_exists"] = app_dir.exists() and app_dir.is_dir()
        
        # Check /app/data directory
        app_data_dir = Path("/app/data")
        debug_info["data_dir_exists"] = app_data_dir.exists() and app_data_dir.is_dir()
        
        # List files in /app/data if it exists
        if debug_info["data_dir_exists"]:
            try:
                data_files = [f.name for f in app_data_dir.iterdir() if f.is_file()]
                debug_info["data_dir_listing"] = sorted(data_files)
            except Exception:
                debug_info["data_dir_listing"] = []
        else:
            debug_info["data_dir_listing"] = []
        
        # List first 30 files in /app if it exists
        if debug_info["app_dir_exists"]:
            try:
                app_files = [f.name for f in app_dir.iterdir() if f.is_file()]
                debug_info["repo_root_listing_sample"] = sorted(app_files)[:30]
            except Exception:
                debug_info["repo_root_listing_sample"] = []
        else:
            debug_info["repo_root_listing_sample"] = []
    
    # Validate query parameter
    if not q or len(q.strip()) < 2:
        if debug_mode:
            debug_info["courses_loaded"] = 0
            debug_info["first_5_courses"] = []
            debug_info["matches_found"] = 0
            return {"results": [], "debug": debug_info}
        return {"results": []}
    
    query = q.strip().lower()
    
    # Load courses from courses.json (tries root first, then data/ subdirectory)
    courses = load_courses_from_data(debug_info if debug_mode else None)
    courses_count = len(courses)
    
    if debug_mode:
        debug_info["courses_loaded"] = courses_count
        debug_info["first_5_courses"] = [course["name"] for course in courses[:5]]
    
    if not courses:
        # Determine which path was attempted
        attempted_path = COURSES_PATH if COURSES_PATH.exists() else COURSES_PATH_FALLBACK
        logger.info(f"/courses: q='{q}', path={attempted_path}, loaded=0, matches=0")
        if debug_mode:
            debug_info["matches_found"] = 0
            return {"results": [], "debug": debug_info}
        return {"results": []}
    
    # Perform case-insensitive substring matching
    matches = []
    for course in courses:
        course_name_lower = course["name"].lower()
        if query in course_name_lower:
            matches.append(course)
    
    # Sort results: starts with query first, then contains query
    # Within each group, sort alphabetically by name
    def sort_key(course):
        name_lower = course["name"].lower()
        starts_with = name_lower.startswith(query)
        return (not starts_with, name_lower)  # False (0) comes before True (1), so starts_with=True sorts first
    
    matches.sort(key=sort_key)
    
    # Return only top 8 matches
    top_matches = matches[:8]
    matches_count = len(top_matches)
    
    # Log request details
    # Determine which path was used (primary if exists, otherwise fallback)
    used_path = COURSES_PATH if COURSES_PATH.exists() else COURSES_PATH_FALLBACK
    logger.info(f"/courses: q='{q}', path={used_path}, loaded={courses_count}, matches={matches_count}")
    
    # Return only name and area fields
    results = [{"name": course["name"], "area": course["area"]} for course in top_matches]
    
    if debug_mode:
        debug_info["matches_found"] = matches_count
        return {"results": results, "debug": debug_info}
    
    return {"results": results}


@app.get("/debug/openai")
async def debug_openai() -> Dict[str, Any]:
    """
    Debug endpoint to check OpenAI setup.
    Returns JSON with import status, version, client creation status, and API key presence.
    Must not crash even if imports fail.
    """
    result = {
        "openai_import_ok": False,
        "openai_version": "unknown",
        "client_created": False,
        "has_openai_key": bool(OPENAI_API_KEY)
    }
    
    # Try to import openai
    try:
        import openai
        result["openai_import_ok"] = True
    except Exception:
        result["openai_import_ok"] = False
        return result
    
    # Try to get openai version
    try:
        import importlib.metadata
        version = importlib.metadata.version("openai")
        result["openai_version"] = version
    except Exception:
        try:
            # Fallback to __version__ attribute
            version = getattr(openai, "__version__", "unknown")
            result["openai_version"] = version
        except Exception:
            result["openai_version"] = "unknown"
    
    # Try to instantiate client the same way the app does
    try:
        from openai import AsyncOpenAI
        test_client = AsyncOpenAI(api_key=OPENAI_API_KEY if OPENAI_API_KEY else "test-key", timeout=10.0)
        result["client_created"] = True
    except Exception:
        result["client_created"] = False
    
    return result


@app.get("/", response_class=HTMLResponse)
async def read_root():
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Alba Labs</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600&display=swap" rel="stylesheet">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            :root {{
                --alba-cream: #FFF7E0;
                --alba-yellow: #FBB924;
                --alba-orange: #F78222;
                --alba-red: #E23642;
                --alba-offblack: #2C2C2F;
                --alba-black: #000000;
                --alba-green: #4A9B5A;
            }}
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            html {{
                height: auto;
                margin: 0;
                padding: 0;
                background: var(--alba-offblack);
            }}
            body {{
                height: auto;
                margin: 0;
                padding: 0;
                font-family: 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: var(--alba-offblack);
                line-height: 1.6;
                color: var(--alba-cream);
            }}
            .container {{
                max-width: 1100px;
                width: min(1100px, calc(100vw - 48px));
                margin: 0 auto;
                padding: 24px;
            }}
            @media (max-width: 640px) {{
                .container {{
                    padding: 16px;
                }}
            }}
            .form-card {{
                background: #303035;
                border-radius: 12px;
                padding: 20px;
                border: 1px solid rgba(255, 255, 255, 0.05);
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
            }}
            .form-header {{
                margin-bottom: 16px;
                text-align: center;
            }}
            .form-title {{
                color: var(--alba-cream);
                font-weight: 500;
                font-size: clamp(44px, 48px, 48px);
                line-height: 1.1;
                margin-bottom: 8px;
                letter-spacing: -0.5px;
            }}
            .form-subtitle {{
                color: rgba(255, 247, 224, 0.7);
                font-weight: 300;
                font-size: 17px;
                line-height: 1.4;
            }}
            @media (max-width: 640px) {{
                .form-title {{
                    font-size: 32px;
                }}
                .form-subtitle {{
                    font-size: 16px;
                }}
            }}
            form {{
                margin-top: 0;
            }}
            .form-grid {{
                display: grid;
                grid-template-columns: 1fr;
                gap: 16px;
            }}
            @media (min-width: 900px) {{
                .form-grid {{
                    grid-template-columns: 1fr 1fr;
                }}
            }}
            .form-group {{
                margin-bottom: 0;
            }}
            .form-group-full {{
                grid-column: 1 / -1;
            }}
            @media (min-width: 900px) {{
                .form-group-full {{
                    grid-column: 1 / -1;
                }}
            }}
            .form-row {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 12px;
            }}
            @media (max-width: 640px) {{
                .form-row {{
                    grid-template-columns: 1fr;
                }}
            }}
            label {{
                display: block;
                margin-bottom: 6px;
                font-weight: 500;
                font-size: 13px;
                color: var(--alba-cream);
                letter-spacing: 0.1px;
            }}
            select, input[type="number"], input[type="text"] {{
                width: 100%;
                padding: 10px 14px;
                font-size: 15px;
                font-family: 'Poppins', sans-serif;
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 8px;
                background: var(--alba-offblack);
                transition: all 0.2s ease;
                color: var(--alba-cream);
            }}
            select:focus, input[type="number"]:focus, input[type="text"]:focus {{
                outline: none;
                border-color: var(--alba-orange);
                box-shadow: 0 0 0 3px rgba(247, 130, 34, 0.2);
            }}
            .course-input-wrapper {{
                position: relative;
            }}
            .course-helper {{
                font-size: 11px;
                color: rgba(255, 247, 224, 0.5);
                margin-top: 4px;
                font-weight: 300;
            }}
            .course-chips {{
                display: flex;
                flex-wrap: wrap;
                gap: 4px;
                margin-top: 6px;
            }}
            .course-chip {{
                display: inline-block;
                padding: 3px 8px;
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 10px;
                font-size: 10px;
                color: rgba(255, 247, 224, 0.7);
                cursor: pointer;
                transition: all 0.2s ease;
                font-weight: 400;
            }}
            .course-chip:hover {{
                background: rgba(247, 130, 34, 0.2);
                border-color: var(--alba-orange);
                color: var(--alba-cream);
            }}
            .autocomplete-container {{
                position: relative;
            }}
            .autocomplete-suggestions {{
                position: absolute;
                top: calc(100% + 4px);
                left: 0;
                right: 0;
                background: #303035;
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 8px;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
                max-height: 280px;
                overflow-y: auto;
                z-index: 1000;
                display: none;
            }}
            .autocomplete-suggestions.show {{
                display: block;
            }}
            .autocomplete-suggestion {{
                padding: 12px 16px;
                cursor: pointer;
                transition: background-color 0.15s ease;
                font-size: 15px;
                color: var(--alba-cream);
            }}
            .autocomplete-suggestion:first-child {{
                border-radius: 8px 8px 0 0;
            }}
            .autocomplete-suggestion:last-child {{
                border-radius: 0 0 8px 8px;
            }}
            .autocomplete-suggestion:hover,
            .autocomplete-suggestion.highlighted {{
                background-color: rgba(255, 255, 255, 0.1);
            }}
            .autocomplete-no-matches {{
                padding: 16px;
                color: rgba(255, 247, 224, 0.6);
                font-size: 14px;
                text-align: center;
            }}
            .help-text {{
                font-size: 12px;
                color: rgba(255, 247, 224, 0.6);
                margin-top: 6px;
                font-weight: 300;
                line-height: 1.4;
            }}
            .error-message {{
                font-size: 12px;
                color: var(--alba-red);
                margin-top: 6px;
                font-weight: 400;
                display: none;
            }}
            .error-message.show {{
                display: block;
            }}
            .primary-button {{
                background: var(--alba-orange);
                color: var(--alba-black);
                padding: 12px 24px;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                font-size: 15px;
                font-weight: 500;
                font-family: 'Poppins', sans-serif;
                transition: all 0.2s ease;
                width: 100%;
                margin-top: 0;
            }}
            @media (min-width: 900px) {{
                .primary-button {{
                    width: auto;
                    min-width: 180px;
                }}
            }}
            .primary-button:hover {{
                background: var(--alba-yellow);
                transform: translateY(-1px);
            }}
            .primary-button:active {{
                transform: translateY(0);
            }}
            .button-wrapper {{
                display: flex;
                justify-content: center;
                margin-top: 8px;
            }}
            .build-footer {{
                display: none;
                font-size: 10px;
                color: rgba(255, 247, 224, 0.3);
                text-align: center;
                margin-top: 8px;
                padding-top: 8px;
                font-weight: 300;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="form-card">
                <div class="form-header">
                    <h1 class="form-title">Should I Play Golf Today?</h1>
                    <p class="form-subtitle">A clear, practical breakdown of weather, ground conditions, course pressure, and whether today suits your handicap.</p>
                </div>
                <form method="post" action="/assess">
                    <div class="form-grid">
                        <div class="form-group">
                            <label for="course">Course</label>
                            <div class="course-input-wrapper">
                                <div class="autocomplete-container">
                                    <input type="text" id="course" name="course" placeholder="Start typing a course name" required autocomplete="off">
                                    <div id="autocomplete-suggestions" class="autocomplete-suggestions"></div>
                                </div>
                                <div class="course-helper">Start typing a course name</div>
                                <div class="course-chips">
                                    <span class="course-chip" data-course="Trent Park Golf Club">Trent Park</span>
                                    <span class="course-chip" data-course="Richmond Park Golf Course">Richmond Park</span>
                                    <span class="course-chip" data-course="Dukes Meadows Golf Course">Dukes Meadows</span>
                                </div>
                            </div>
                            <div id="course-error" class="error-message">Please select a course</div>
                        </div>
                        
                        <div class="form-group">
                            <label for="handicap">Handicap</label>
                            <input type="number" id="handicap" name="handicap" min="0" max="54" value="25" required>
                            <div class="help-text">Enter your handicap (0 to 54). Beginners typically start around 25-30.</div>
                            
                            <div class="form-row" style="margin-top: 16px;">
                                <div class="form-group" style="margin-bottom: 0;">
                                    <label for="day">Day</label>
                                    <select id="day" name="day" required>
                                        <option value="Today">Today</option>
                                        <option value="Tomorrow">Tomorrow</option>
                                    </select>
                                </div>
                                
                                <div class="form-group" style="margin-bottom: 0;">
                                    <label for="time_of_day">Time of day</label>
                                    <select id="time_of_day" name="time_of_day" required>
                                        <option value="Morning">Morning</option>
                                        <option value="Midday">Midday</option>
                                        <option value="Afternoon">Afternoon</option>
                                        <option value="Evening">Evening</option>
                                    </select>
                                </div>
                            </div>
                        </div>
                        
                        <div class="form-group form-group-full">
                            <div class="button-wrapper">
                                <button type="submit" class="primary-button">Check playability</button>
                            </div>
                        </div>
                    </div>
                </form>
            </div>
        <script>
            (function() {{
                const courseInput = document.getElementById('course');
                const suggestionsContainer = document.getElementById('autocomplete-suggestions');
                let currentHighlight = -1;
                let suggestions = [];
                
                function fetchSuggestions(query) {{
                    if (query.length < 2) {{
                        hideSuggestions();
                        return;
                    }}
                    
                    fetch('/courses?q=' + encodeURIComponent(query))
                        .then(response => response.json())
                        .then(data => {{
                            suggestions = data.results || [];
                            displaySuggestions(suggestions);
                        }})
                        .catch(error => {{
                            console.error('Error fetching suggestions:', error);
                            hideSuggestions();
                        }});
                }}
                
                function displaySuggestions(results) {{
                    // Temporary console log for debugging
                    console.log('Raw API results:', results);
                    
                    suggestionsContainer.innerHTML = '';
                    currentHighlight = -1;
                    
                    if (results.length === 0) {{
                        const noMatches = document.createElement('div');
                        noMatches.className = 'autocomplete-no-matches';
                        noMatches.textContent = 'No matches. Try a nearby area.';
                        suggestionsContainer.appendChild(noMatches);
                        suggestionsContainer.classList.add('show');
                        return;
                    }}
                    
                    results.forEach((course, index) => {{
                        const suggestion = document.createElement('div');
                        suggestion.className = 'autocomplete-suggestion';
                        
                        // Render full name and area (only show area in brackets if not empty)
                        const displayText = course.area && course.area.trim() !== '' 
                            ? `${{course.name}} (${{course.area}})` 
                            : course.name;
                        suggestion.textContent = displayText;
                        
                        suggestion.dataset.index = index;
                        suggestion.dataset.name = course.name;
                        
                        suggestion.addEventListener('click', () => {{
                            selectSuggestion(course.name);
                        }});
                        
                        suggestionsContainer.appendChild(suggestion);
                    }});
                    
                    suggestionsContainer.classList.add('show');
                }}
                
                function hideSuggestions() {{
                    suggestionsContainer.classList.remove('show');
                    currentHighlight = -1;
                }}
                
                function selectSuggestion(courseName) {{
                    // Temporary console log for debugging
                    console.log('Selected course name written to input:', courseName);
                    courseInput.value = courseName;
                    hideSuggestions();
                }}
                
                function highlightSuggestion(index) {{
                    const items = suggestionsContainer.querySelectorAll('.autocomplete-suggestion');
                    items.forEach((item, i) => {{
                        if (i === index) {{
                            item.classList.add('highlighted');
                            item.scrollIntoView({{ block: 'nearest' }});
                        }} else {{
                            item.classList.remove('highlighted');
                        }}
                    }});
                }}
                
                courseInput.addEventListener('input', (e) => {{
                    const query = e.target.value.trim();
                    fetchSuggestions(query);
                }});
                
                courseInput.addEventListener('keydown', (e) => {{
                    const items = suggestionsContainer.querySelectorAll('.autocomplete-suggestion');
                    
                    if (!suggestionsContainer.classList.contains('show') || items.length === 0) {{
                        return;
                    }}
                    
                    if (e.key === 'ArrowDown') {{
                        e.preventDefault();
                        currentHighlight = Math.min(currentHighlight + 1, items.length - 1);
                        highlightSuggestion(currentHighlight);
                    }} else if (e.key === 'ArrowUp') {{
                        e.preventDefault();
                        currentHighlight = Math.max(currentHighlight - 1, -1);
                        if (currentHighlight >= 0) {{
                            highlightSuggestion(currentHighlight);
                        }} else {{
                            items.forEach(item => item.classList.remove('highlighted'));
                        }}
                    }} else if (e.key === 'Enter') {{
                        e.preventDefault();
                        if (currentHighlight >= 0 && currentHighlight < suggestions.length) {{
                            selectSuggestion(suggestions[currentHighlight].name);
                        }}
                    }} else if (e.key === 'Escape') {{
                        hideSuggestions();
                    }}
                }});
                
                document.addEventListener('click', (e) => {{
                    if (!courseInput.contains(e.target) && !suggestionsContainer.contains(e.target)) {{
                        hideSuggestions();
                    }}
                }});
                
                // Form validation
                const form = document.querySelector('form');
                const courseError = document.getElementById('course-error');
                
                function validateCourse() {{
                    const courseValue = courseInput.value.trim();
                    if (courseValue === '') {{
                        courseError.classList.add('show');
                        return false;
                    }} else {{
                        courseError.classList.remove('show');
                        return true;
                    }}
                }}
                
                form.addEventListener('submit', function(e) {{
                    if (!validateCourse()) {{
                        e.preventDefault();
                        courseInput.focus();
                    }}
                }});
                
                courseInput.addEventListener('input', function() {{
                    if (courseInput.value.trim() !== '') {{
                        courseError.classList.remove('show');
                    }}
                }});
                
                // Course chip click handlers
                const courseChips = document.querySelectorAll('.course-chip');
                courseChips.forEach(chip => {{
                    chip.addEventListener('click', function() {{
                        const courseName = this.getAttribute('data-course');
                        courseInput.value = courseName;
                        courseError.classList.remove('show');
                        hideSuggestions();
                    }});
                }});
            }})();
        </script>
            </div>
        </div>
        <div class="build-footer">Build: {BUILD_TIME_UTC}</div>
    </body>
    </html>
    """


async def render_assessment_results(course: str, handicap: int, day: str, time_of_day: str, force_llm: bool = False, llm_effective_enabled: bool = False, llm_raw=None, request_id: str = None, debug_mode: bool = False):
    """
    Shared function to calculate ratings and render assessment results.
    
    force_llm: parsed llm query parameter (boolean)
    llm_effective_enabled: computed effective LLM enabled status
    llm_raw: raw llm query parameter value for debugging
    request_id: unique request identifier for logging
    """
    # Find the course to get coordinates and properties
    course_data = find_course_by_name(course)
    
    # Calculate target date and day of week
    today = datetime.now().date()
    if day == "Tomorrow":
        target_date = (today + timedelta(days=1)).isoformat()
        target_datetime = datetime.now() + timedelta(days=1)
    else:
        target_date = today.isoformat()
        target_datetime = datetime.now()
    
    day_of_week = target_datetime.weekday()  # 0=Monday, 6=Sunday
    month = target_datetime.month
    
    # Fetch weather data
    weather_data = None
    tomorrow_weather = None
    historical_rainfall = None
    
    if course_data:
        lat = course_data["lat"]
        lon = course_data["lon"]
        weather_data = await fetch_weather_data(lat, lon, target_date)
        historical_rainfall = await fetch_historical_rainfall(lat, lon, 7)
        
        # If today, fetch tomorrow for comparison
        if day == "Today":
            tomorrow_weather = await fetch_tomorrow_weather(lat, lon)
    
    # Calculate all ratings
    weather_info = calculate_weather_label(weather_data)
    weather_rating = weather_info["weather_rating"]  # For backward compatibility
    weather_label = weather_info["weather_label"]  # New condition-based label
    
    ground_info = calculate_ground_condition(historical_rainfall)
    ground_label = ground_info["ground_label"] if isinstance(ground_info, dict) else ground_info
    
    tomorrow_forecast = None
    if day == "Today" and tomorrow_weather:
        tomorrow_forecast = calculate_tomorrow_forecast(weather_data, tomorrow_weather)
    
    # Calculate course pressure (busyness)
    busyness_info = calculate_course_pressure(
        month, weather_rating, day_of_week, time_of_day, 
        course_data["popularity_tier"] if course_data else "Medium"
    )
    busyness_label = busyness_info["busyness_label"]
    busyness_rating = busyness_label  # For backward compatibility
    
    # Get course difficulty for scoring
    course_difficulty = course_data["difficulty"] if course_data else "Medium"
    
    # Calculate daylight feasibility
    daylight_info = calculate_daylight_feasibility(
        time_of_day, busyness_rating, weather_data, month, target_date
    )
    recommended_holes = daylight_info["recommended_holes"]
    daylight_label = daylight_info["daylight_label"]
    finish_time_estimate = daylight_info["finish_time_estimate"]
    sunset_time = daylight_info["sunset_time"]
    
    # Get price tier for scoring
    price_tier_raw = course_data["price_tier"] if course_data else "££"
    
    # Compute playability using deterministic scoring model
    playability = compute_playability(
        weather_data, ground_info, busyness_info, course_difficulty,
        daylight_label, handicap, recommended_holes, price_tier_raw
    )
    
    # Extract weather info from playability results
    weather_info = playability.get("weather_info", weather_info)
    weather_label = weather_info["weather_label"]
    
    # Extract suitability label from playability results for display
    # Use normalized format: "Good for a {handicap} handicap" or "Tough for a {handicap} handicap today"
    suitability_reasons_list = [r for r in playability["reasons"] if r["factor"] == "suitability"]
    if suitability_reasons_list:
        suitability_label_raw = suitability_reasons_list[0]["condition"] if suitability_reasons_list else ""
    else:
        suitability_label_raw = ""
    
    # Normalize suitability label for display
    suitability_label_display = normalize_suitability_label_for_display(suitability_label_raw, handicap)
    
    # Keep old handicap_suitability for backward compatibility (used in some display code)
    suitability_score = playability["factor_scores"]["suitability"]
    if suitability_score >= 60:
        handicap_suitability = "Well suited"
    else:
        handicap_suitability = "Not ideal today"
    
    # Extract structured outputs
    overall_score = playability["overall_score"]
    verdict = playability["verdict"]
    reasons = playability["reasons"]
    recommendations = playability["recommendations"]
    
    # Try LLM rewrite if enabled (only rewrites for clarity, doesn't invent)
    llm_rewrite_succeeded = False
    if llm_effective_enabled:
        try:
            deterministic_data = {
                "reasons": reasons,
                "recommendations": recommendations,
                "verdict": verdict,
                "course_name": course,
                "handicap": handicap
            }
            rewritten_data = await rewrite_reasons_and_recommendations_llm(deterministic_data, request_id)
            # Use rewritten versions
            reasons = rewritten_data["reasons"]
            recommendations = rewritten_data["recommendations"]
            llm_rewrite_succeeded = True
            logger.info(f"LLM rewrite succeeded for reasons/recommendations")
        except Exception as e:
            # Fallback to deterministic on any error
            logger.error(f"LLM rewrite failed, using deterministic: {str(e)}", exc_info=True)
            # reasons and recommendations remain as deterministic versions
    
    # Map verdict to play_recommendation for backward compatibility
    if verdict == "Play":
        play_recommendation = "Play"
    else:
        play_recommendation = "Don't play"
    
    # Generate handicap-aware why bullets
    # Format: "[Factor]: [what's happening], so at a {handicap} handicap you can expect [impact]."
    # Always returns 3-4 bullets, explicitly tied to handicap
    why_bullets = generate_handicap_aware_why_bullets(
        reasons, handicap, weather_label, ground_label, busyness_label
    )
    
    # Generate what to do bullets from structured recommendations
    # Format: "[Action]. [Why tied to handicap]."
    # Rules: 2-4 bullets, each starts with action, includes WHY tied to handicap, one sentence, ends with full stop
    # Remove score references from user-facing text
    what_to_do_bullets = []
    seen_actions = set()
    
    if recommendations:
        for rec in recommendations:
            action = rec.get("action", "")
            
            if not action:
                continue
            
            # Format bullet: "[Action]." (separate sentence, actions only, not analysis)
            # Ensure action ends with period if it doesn't already
            action_clean = action.strip()
            if not action_clean.endswith('.'):
                action_clean += "."
            
            # Use action only (not analysis/reason) - format as separate bullet sentence
            bullet = action_clean
            
            # Deduplicate by action
            action_normalized = action.lower().strip()
            if action_normalized not in seen_actions:
                seen_actions.add(action_normalized)
                what_to_do_bullets.append(bullet)
            
            # Limit to 4 bullets max
            if len(what_to_do_bullets) >= 4:
                break
    
    # Ensure we have at least 2 bullets
    if len(what_to_do_bullets) < 2:
        if verdict == "Play":
            # Fallback for Play verdict
            what_to_do_bullets.append("Enjoy your round. Conditions are suitable for your handicap today.")
        else:
            # Fallback for Not ideal
            what_to_do_bullets.append(f"Consider waiting for better conditions. Today's conditions add unnecessary challenge for your handicap of {handicap}.")
    
    # Limit to 4 bullets max
    what_to_do_bullets = what_to_do_bullets[:4]
    
    # Generate added_action from recommendations (for LLM summary)
    if recommendations:
        added_action = ". ".join([rec["action"] + ". " + rec["reason"] for rec in recommendations[:2]]) + "."
    else:
        added_action = ""
    
    # Convert price_tier to price_label (price_tier_raw already set above)
    price_label = get_price_label(price_tier_raw)
    
    # Generate summary from structured playability outputs
    # Use top reason and verdict to create a concise summary
    if reasons:
        top_reason = reasons[0]["impact"]
        if verdict == "Play":
            final_summary = f"Good conditions today. {top_reason}."
        else:  # Not ideal
            final_summary = f"Conditions are not ideal today. {top_reason}."
    else:
        # Fallback (shouldn't happen)
        final_summary = f"Overall score: {overall_score}/100. Verdict: {verdict}."
    
    # Determine summary_mode based on whether LLM rewrite succeeded
    # Note: LLM now only rewrites reasons/recommendations, not summaries
    # Summary is generated from deterministic reasons (which may be LLM-rewritten)
    if llm_rewrite_succeeded:
        summary_mode = "LLM (rewritten)"
    elif llm_effective_enabled:
        summary_mode = "Deterministic (LLM failed)"
    else:
        summary_mode = "Deterministic"
    
    # Build HTML sections in exact order specified
    # 1. Weather rating - use normalized labels for display
    # Normalize weather label to user-friendly display values
    weather_label_display = normalize_weather_label_for_display(weather_label, weather_data)
    ground_label_display = get_ground_label(ground_info)
    weather_rating_html = f"""
        <div class="result-item">
            <div class="result-label">Weather Rating</div>
            <div class="result-value">{weather_label_display}</div>
        </div>
        <div class="result-item">
            <div class="result-label">Ground</div>
            <div class="result-value">{ground_label_display}</div>
        </div>
    """
    if tomorrow_forecast:
        weather_rating_html += f"""
        <div class="result-item">
            <div class="result-label">Tomorrow Forecast</div>
            <div class="result-value">{tomorrow_forecast}</div>
        </div>
        """
    
    # 2. Busyness rating
    busyness_html = f"""
        <div class="result-item">
            <div class="result-label">Busyness Rating</div>
            <div class="result-value">{busyness_rating}</div>
            <div class="help-text">Busyness estimate (not live tee times)</div>
        </div>
    """
    
    # 3. Handicap suitability - use explicit label from scoring model (already set above)
    handicap_html = f"""
        <div class="result-item">
            <div class="result-label">Handicap Suitability</div>
            <div class="result-value">{suitability_label_display}</div>
        </div>
    """
    
    # 3.5. Daylight feasibility
    daylight_html = f"""
        <div class="result-item">
            <div class="result-label">Recommended Holes</div>
            <div class="result-value">{recommended_holes} holes</div>
        </div>
        <div class="result-item">
            <div class="result-label">Daylight</div>
            <div class="result-value">{daylight_label}</div>
        </div>
        <div class="result-item">
            <div class="result-label">Timing</div>
            <div class="result-value">Estimated finish: {finish_time_estimate}, Sunset: {sunset_time}</div>
        </div>
    """
    
    # 4. Price tier - use descriptive label instead of symbols
    price_tier_raw = course_data["price_tier"] if course_data else "££"
    price_label_display = get_price_label(price_tier_raw)
    price_html = f"""
        <div class="result-item">
            <div class="result-label">Price Tier</div>
            <div class="result-value">{price_label_display}</div>
            <div class="help-text">Typical estimate</div>
        </div>
    """
    
    # Generate banner content
    # Status pill: single decision label
    if verdict == "Play":
        status_pill_text = "YES, PLAY"
    else:  # Not ideal
        status_pill_text = "NOT TODAY"
    
    # Main headline: course name
    verdict_title = course
    
    # Generate summary sentence from top drivers
    banner_summary = generate_banner_summary(
        reasons, verdict, handicap, weather_label_display, 
        ground_label_display, busyness_label, suitability_label_display
    )
    
    # Why section - use handicap-aware bullets (always 3-4 bullets)
    # Ensure we have 3-4 bullets (function should always return at least 3)
    why_bullets_final = why_bullets[:4] if len(why_bullets) >= 3 else why_bullets
    
    # Fallback if we somehow have fewer than 3 bullets (shouldn't happen, but safety check)
    if len(why_bullets_final) < 3:
        # Regenerate with all available data
        why_bullets_final = generate_handicap_aware_why_bullets(
            reasons, handicap, weather_label, ground_label, busyness_label
        )
        why_bullets_final = why_bullets_final[:4]
    
    # Render as HTML
    why_bullets_html = '<ul class="why-bullets">' + ''.join([f'<li>{bullet}</li>' for bullet in why_bullets_final]) + '</ul>'
    
    # What to do instead - use formatted bullets array (2-4 bullets)
    # Ensure we have 2-4 bullets
    what_to_do_bullets_final = what_to_do_bullets[:4] if len(what_to_do_bullets) >= 2 else what_to_do_bullets
    
    # Render as HTML
    what_bullets_html = '<ul class="what-bullets">' + ''.join([f'<li>{bullet}</li>' for bullet in what_to_do_bullets_final]) + '</ul>'
    
    # What section title
    what_section_title = "What to expect" if play_recommendation == "Play" else "What to do instead"
    
    # Verdict banner with status pill, course name and helper text
    # Use structured verdict (binary decision)
    if verdict == "Play":
        verdict_banner_class = "play"
    else:  # Not ideal
        verdict_banner_class = "dont-play"
    verdict_banner_html = f"""
        <div class="verdict-banner {verdict_banner_class}">
            <div class="verdict-content">
                <div class="status-pill">{status_pill_text}</div>
                <div class="verdict-info">
                    <div class="verdict-title-row">
                        <div class="verdict-title">{verdict_title}</div>
                    </div>
                    <div class="verdict-helper">{banner_summary}</div>
                </div>
            </div>
        </div>
    """
    
    # Details section - collapsible - use explicit labels
    details_html = f"""
        <details class="details-card">
            <summary class="details-summary">Show details</summary>
            <div class="details-grid">
                <div class="detail-item">
                    <div class="detail-label">
                        <i data-lucide="cloud-rain"></i>
                        <span>Weather</span>
                    </div>
                    <div class="detail-value">{weather_label_display}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">
                        <i data-lucide="droplets"></i>
                        <span>Ground</span>
                    </div>
                    <div class="detail-value">{ground_label_display}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">
                        <i data-lucide="users"></i>
                        <span>Busyness</span>
                    </div>
                    <div class="detail-value">{busyness_rating}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">
                        <i data-lucide="target"></i>
                        <span>Handicap fit</span>
                    </div>
                    <div class="detail-value">{suitability_label_display}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">
                        <i data-lucide="pound-sterling"></i>
                        <span>Price</span>
                    </div>
                    <div class="detail-value">{price_label_display}</div>
                </div>
            </div>
        </details>
    """
    
    # Debug info (only shown if debug_mode is True)
    if debug_mode:
        factor_scores = playability.get("factor_scores", {})
        debug_info_html = f"""
        <div class="debug-info" style="margin-top: 20px; padding: 16px; background: rgba(255,255,255,0.05); border-radius: 8px; font-size: 12px; font-family: monospace;">
            <strong>Scoring Model Outputs:</strong><br>
            Overall Score: {overall_score}/100<br>
            Verdict: {verdict}<br>
            Factor Scores:<br>
            {chr(10).join([f"  {factor}: {score}/100" for factor, score in factor_scores.items()])}<br>
            <br>
            <strong>Raw Parameters:</strong><br>
            Weather: {weather_rating}<br>
            Ground: {ground_label}<br>
            Busyness: {busyness_rating}<br>
            Suitability: {handicap_suitability}<br>
            Price Tier: {price_tier_raw}<br>
            Daylight: {daylight_label}<br>
            Handicap: {handicap}<br>
            Recommended Holes: {recommended_holes}<br>
        </div>
        """
    else:
        debug_info_html = ""
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Assessment Results - Alba Labs</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600&display=swap" rel="stylesheet">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://unpkg.com/lucide@latest"></script>
        <style>
            :root {{
                --alba-cream: #FFF7E0;
                --alba-yellow: #FBB924;
                --alba-orange: #F78222;
                --alba-red: #E23642;
                --alba-offblack: #2C2C2F;
                --alba-black: #000000;
                --alba-green: #1F8A4C;
            }}
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            html {{
                height: auto;
                margin: 0;
                padding: 0;
                background: var(--alba-offblack);
            }}
            body {{
                height: auto;
                margin: 0;
                padding: 0;
                font-family: 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: var(--alba-offblack);
                line-height: 1.6;
                color: var(--alba-cream);
            }}
            .container {{
                max-width: 1100px;
                width: min(1100px, calc(100vw - 48px));
                margin: 0 auto;
                padding: 24px;
            }}
            @media (max-width: 640px) {{
                .container {{
                    padding: 16px;
                }}
            }}
            .page-header {{
                margin-bottom: 20px;
                text-align: center;
            }}
            .page-title {{
                color: var(--alba-cream);
                font-weight: 500;
                font-size: clamp(44px, 48px, 48px);
                line-height: 1.1;
                margin-bottom: 8px;
                letter-spacing: -0.5px;
            }}
            .page-subtitle {{
                color: rgba(255, 247, 224, 0.7);
                font-weight: 300;
                font-size: 17px;
                line-height: 1.4;
            }}
            @media (max-width: 640px) {{
                .page-title {{
                    font-size: 32px;
                }}
                .page-subtitle {{
                    font-size: 16px;
                }}
            }}
            .verdict-banner {{
                border-radius: 8px;
                padding: 14px 18px;
                margin-bottom: 16px;
            }}
            .verdict-banner.play {{
                background: var(--alba-green);
            }}
            .verdict-banner.dont-play {{
                background: var(--alba-red);
            }}
            .verdict-content {{
                display: flex;
                align-items: flex-start;
                gap: 16px;
            }}
            .status-pill {{
                display: inline-block;
                padding: 4px 10px;
                border-radius: 12px;
                font-size: 11px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                background: rgba(0, 0, 0, 0.2);
                color: var(--alba-cream);
                white-space: nowrap;
                flex-shrink: 0;
            }}
            .verdict-info {{
                flex: 1;
                min-width: 0;
            }}
            .verdict-title-row {{
                margin-bottom: 6px;
            }}
            .verdict-title {{
                color: var(--alba-cream);
                font-weight: 600;
                font-size: 18px;
                flex: 1;
                min-width: 0;
            }}
            .verdict-course {{
                color: var(--alba-cream);
                font-weight: 500;
                font-size: 16px;
                margin-bottom: 6px;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }}
            .verdict-helper {{
                color: rgba(255, 247, 224, 0.9);
                font-weight: 300;
                font-size: 13px;
                line-height: 1.5;
                display: -webkit-box;
                -webkit-line-clamp: 2;
                -webkit-box-orient: vertical;
                overflow: hidden;
                margin-bottom: 0;
            }}
            @media (max-width: 640px) {{
                .verdict-banner {{
                    padding: 12px 16px;
                }}
                .verdict-content {{
                    flex-direction: column;
                    gap: 10px;
                }}
                .verdict-title {{
                    font-size: 16px;
                }}
                .verdict-course {{
                    font-size: 14px;
                    white-space: normal;
                }}
                .verdict-helper {{
                    font-size: 12px;
                    -webkit-line-clamp: 3;
                }}
            }}
            .cards-grid {{
                display: grid;
                grid-template-columns: 1fr;
                gap: 16px;
                margin-bottom: 16px;
            }}
            @media (min-width: 900px) {{
                .cards-grid {{
                    grid-template-columns: 1fr 1fr;
                }}
            }}
            .card {{
                padding: 18px 20px;
                background: #303035;
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 8px;
                box-shadow: 0 1px 4px rgba(0, 0, 0, 0.2);
                transition: transform 0.2s ease, box-shadow 0.2s ease;
            }}
            .card:hover {{
                transform: translateY(-1px);
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
            }}
            .card-title {{
                font-weight: 700;
                color: var(--alba-cream);
                margin-bottom: 14px;
                font-size: 14px;
                letter-spacing: -0.1px;
            }}
            .card-content {{
                color: var(--alba-cream);
                font-size: 14px;
                font-weight: 400;
                line-height: 1.7;
            }}
            .why-bullets, .what-bullets {{
                list-style: none;
                padding: 0;
                margin: 0;
            }}
            .why-bullets li, .what-bullets li {{
                padding: 8px 0;
                padding-left: 20px;
                position: relative;
                line-height: 1.75;
            }}
            .why-bullets li:before {{
                content: "•";
                position: absolute;
                left: 6px;
                color: var(--alba-orange);
                font-size: 16px;
                line-height: 1.4;
            }}
            .what-bullets li:before {{
                content: "•";
                position: absolute;
                left: 6px;
                color: var(--alba-orange);
                font-size: 16px;
                line-height: 1.4;
            }}
            .card-content p {{
                margin: 0;
            }}
            .details-card {{
                grid-column: 1 / -1;
                background: #303035;
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 8px;
                box-shadow: 0 1px 4px rgba(0, 0, 0, 0.2);
                overflow: hidden;
            }}
            .details-summary {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                width: 100%;
                padding: 14px 18px;
                font-weight: 600;
                color: var(--alba-cream);
                font-size: 15px;
                cursor: pointer;
                list-style: none;
                letter-spacing: -0.1px;
                background: transparent;
                border: none;
                transition: background-color 0.2s ease;
            }}
            .details-summary:hover {{
                background: rgba(255, 255, 255, 0.03);
            }}
            .details-summary::-webkit-details-marker {{
                display: none;
            }}
            .details-summary::after {{
                content: "▶";
                display: inline-block;
                font-size: 12px;
                transition: transform 0.3s ease;
                margin-left: auto;
            }}
            .details-card[open] .details-summary::after {{
                transform: rotate(90deg);
            }}
            .details-grid {{
                display: grid;
                grid-template-columns: repeat(5, 1fr);
                gap: 12px;
                padding: 16px 18px;
                animation: slideDown 0.3s ease-out;
            }}
            @keyframes slideDown {{
                from {{
                    opacity: 0;
                    transform: translateY(-8px);
                }}
                to {{
                    opacity: 1;
                    transform: translateY(0);
                }}
            }}
            @media (max-width: 900px) {{
                .details-grid {{
                    grid-template-columns: repeat(2, 1fr);
                }}
            }}
            @media (max-width: 640px) {{
                .details-grid {{
                    grid-template-columns: repeat(2, 1fr);
                    gap: 10px;
                    padding: 14px 16px;
                }}
            }}
            .detail-item {{
                padding: 10px 12px;
                background: rgba(0, 0, 0, 0.2);
                border-radius: 6px;
                border: 1px solid transparent;
                transition: border-color 0.2s ease, background-color 0.2s ease;
            }}
            .detail-item:hover {{
                border-color: rgba(247, 130, 34, 0.3);
                background: rgba(0, 0, 0, 0.25);
            }}
            .detail-label {{
                display: flex;
                align-items: center;
                gap: 8px;
                font-weight: 500;
                color: rgba(255, 247, 224, 0.6);
                font-size: 10px;
                text-transform: uppercase;
                letter-spacing: 0.8px;
                margin-bottom: 5px;
                line-height: 1;
            }}
            .detail-label i {{
                width: 19px;
                height: 19px;
                flex-shrink: 0;
                color: inherit;
                opacity: 0.6;
                display: flex;
                align-items: center;
                justify-content: center;
            }}
            .detail-label i svg {{
                width: 100%;
                height: 100%;
                stroke-width: 1.75;
            }}
            .detail-label span {{
                flex: 1;
                line-height: 1;
            }}
            .detail-value {{
                color: var(--alba-cream);
                font-size: 16px;
                font-weight: 600;
                line-height: 1.4;
                margin-top: 0;
            }}
            .back-link {{
                display: inline-block;
                margin-top: 16px;
                color: var(--alba-yellow);
                text-decoration: none;
                font-weight: 500;
                font-size: 14px;
                transition: color 0.2s ease;
            }}
            .back-link:hover {{
                color: var(--alba-orange);
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="page-header">
                <h1 class="page-title">Should I Play Golf Today?</h1>
                <p class="page-subtitle">A clear, practical breakdown of weather, ground conditions, course pressure, and whether today suits your handicap.</p>
            </div>
            {verdict_banner_html}
            
            <div class="cards-grid">
                <div class="card">
                    <div class="card-title">Why</div>
                    <div class="card-content">
                        {why_bullets_html}
                    </div>
                </div>
                
                <div class="card">
                    <div class="card-title">{what_section_title}</div>
                    <div class="card-content">
                        {what_bullets_html}
                    </div>
                </div>
                
                {details_html}
            </div>
            
            <a href="/" class="back-link">← Back</a>
            
            {debug_info_html}
        </div>
        <script>
            (function() {{
                function initIcons() {{
                    if (typeof lucide !== 'undefined') {{
                        lucide.createIcons({{
                            attrs: {{
                                width: 19,
                                height: 19,
                                strokeWidth: 1.75
                            }}
                        }});
                    }}
                }}
                
                // Initialize icons when DOM is ready
                if (document.readyState === 'loading') {{
                    document.addEventListener('DOMContentLoaded', initIcons);
                }} else {{
                    initIcons();
                }}
                
                const detailsCard = document.querySelector('.details-card');
                if (detailsCard) {{
                    const summary = detailsCard.querySelector('.details-summary');
                    if (summary) {{
                        // Store original text content
                        const showText = 'Show details';
                        const hideText = 'Hide details';
                        
                        detailsCard.addEventListener('toggle', function() {{
                            if (detailsCard.open) {{
                                summary.textContent = hideText;
                                // Reinitialize icons after details are opened
                                setTimeout(initIcons, 10);
                            }} else {{
                                summary.textContent = showText;
                            }}
                        }});
                    }}
                }}
            }})();
        </script>
    </body>
    </html>
    """


@app.post("/assess", response_class=RedirectResponse)
async def assess_post(
    course: str = Form(...),
    handicap: int = Form(...),
    day: str = Form(...),
    time_of_day: str = Form(...)
):
    """
    Handle POST form submission and redirect to GET with query parameters.
    """
    # Build query parameters
    params = {
        "course": course,
        "handicap": str(handicap),
        "day": day,
        "time_of_day": time_of_day
    }
    
    # Redirect to GET endpoint
    query_string = urlencode(params)
    return RedirectResponse(url=f"/assess?{query_string}", status_code=303)


def parse_llm_parameter(llm_value) -> bool:
    """
    Parse llm query parameter safely.
    Treats 1, "1", "true", "True", "yes" as True. Everything else False.
    """
    if llm_value is None:
        return False
    
    # Convert to string for comparison
    llm_str = str(llm_value).strip().lower()
    
    # Check for true values
    if llm_str in ["1", "true", "yes"]:
        return True
    
    # Check if it's the integer 1
    try:
        if int(llm_value) == 1:
            return True
    except (ValueError, TypeError):
        pass
    
    return False


@app.get("/assess", response_class=HTMLResponse)
async def assess_get(
    course: str = Query(None),
    handicap: int = Query(None),
    day: str = Query(None),
    time_of_day: str = Query(None),
    llm: str = Query(None, description="Set to 1, '1', 'true', 'True', or 'yes' to force LLM summary"),
    debug: str = Query(None, description="Set to 1 to show debug information")
):
    """
    Handle GET request for assessment results.
    """
    # Validate required parameters
    # Check if course is missing or blank (after stripping whitespace)
    if not course or not course.strip():
        return RedirectResponse(url="/", status_code=303)
    
    if not all([handicap is not None, day, time_of_day]):
        return RedirectResponse(url="/", status_code=303)
    
    # Parse llm parameter safely
    llm_raw = llm
    llm_force = parse_llm_parameter(llm)
    
    # Parse debug parameter
    debug_mode = debug == "1"
    
    # Generate unique request ID for this request
    request_id = str(uuid4())
    
    # Compute llm_effective_enabled = has_openai_key and (llm_force or env_flag_true)
    has_openai_key = bool(OPENAI_API_KEY)
    llm_effective_enabled = has_openai_key and (llm_force or LLM_SUMMARY_ENABLED)
    
    # Log assessment start with debug info
    logger.info(f"ASSESS: request_id={request_id} llm_query={llm_raw} llm_flag={LLM_SUMMARY_ENABLED} has_key={has_openai_key} effective={llm_effective_enabled}")
    
    # Render results
    return await render_assessment_results(course, handicap, day, time_of_day, llm_force, llm_effective_enabled, llm_raw, request_id, debug_mode)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


# Test Cases for Sanity Checking:
#
# Test Case 1: Low handicap golfer, easy course, good weather, quiet conditions
#   Course: Red Libbets Golf Club (Easy, Low popularity, £)
#   Handicap: 8
#   Day: Today
#   Time: Morning
#   Expected: Weather Good/Mixed, Ground Firm/Mixed, Busyness Quiet/Moderate,
#             Handicap Suitability Well suited, Recommendation Play
#
# Test Case 2: High handicap golfer, hard course, busy weekend, poor weather
#   Course: Stanmore Golf Club (Hard, High popularity, £££)
#   Handicap: 32
#   Day: Tomorrow (assuming weekend)
#   Time: Midday
#   Expected: Weather Mixed/Poor, Ground Mixed/Soft, Busyness Busy/Very busy,
#             Handicap Suitability Not ideal today, Recommendation Don't play
#
# Test Case 3: Medium handicap, medium course, moderate conditions
#   Course: Trent Park Golf Club (Medium, Medium popularity, ££)
#   Handicap: 18
#   Day: Today
#   Time: Afternoon
#   Expected: Weather Mixed, Ground Mixed, Busyness Moderate/Busy,
#             Handicap Suitability Well suited/Not ideal, Recommendation depends on weather

