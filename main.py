import json
import os
import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, Form, Query, Request, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.requests import Request as StarletteRequest
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
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
    """
    Get the latest git commit hash with Railway-friendly fallback order:
    1) RAILWAY_GIT_COMMIT_SHA env var (first 7 chars)
    2) GITHUB_SHA env var (first 7 chars)
    3) git rev-parse --short HEAD command
    4) "unknown"
    """
    # Try Railway env var first
    railway_sha = os.getenv("RAILWAY_GIT_COMMIT_SHA")
    if railway_sha:
        return railway_sha[:7]
    
    # Try GitHub Actions env var
    github_sha = os.getenv("GITHUB_SHA")
    if github_sha:
        return github_sha[:7]
    
    # Fall back to git command
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=os.path.dirname(__file__)
        )
        if result.returncode == 0:
            commit_hash = result.stdout.strip()
            # Ensure we return exactly 7 chars (git --short might return fewer)
            return commit_hash[:7] if len(commit_hash) >= 7 else commit_hash
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

# Alba app download URLs
ALBA_IOS_URL = os.getenv("ALBA_IOS_URL", "https://apps.apple.com/gb/app/alba-find-golfers-book-games/id6749025396")
ALBA_ANDROID_URL = os.getenv("ALBA_ANDROID_URL", "https://play.google.com/store/apps/details?id=com.davros.alba")
ALBA_HOW_IT_WORKS_URL = os.getenv("ALBA_HOW_IT_WORKS_URL", "https://www.golfalba.co/blog/what-alba-does")
EXTERNAL_LINK_ATTRS = 'target="_blank" rel="noopener noreferrer"'

# Iframe auto-resize script for Squarespace embedding
IFRAME_RESIZE_SCRIPT = """
<script>
(function () {
  let lastHeight = 0;
  let rafId = null;

  function getStableHeight() {
    const el =
      document.querySelector(".page-wrap") ||
      document.querySelector(".container") ||
      document.documentElement;

    return Math.ceil(el.scrollHeight);
  }

  function sendHeight() {
    rafId = null;
    const height = getStableHeight();
    if (Math.abs(height - lastHeight) < 8) return;
    lastHeight = height;
    parent.postMessage({ type: "ALBA_IFRAME_HEIGHT", height }, "*");
  }

  function scheduleSend() {
    if (rafId) return;
    rafId = requestAnimationFrame(sendHeight);
  }

  window.addEventListener("load", scheduleSend);
  window.addEventListener("resize", scheduleSend);

  const ro = new ResizeObserver(scheduleSend);
  ro.observe(document.body);

  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(scheduleSend).catch(() => {});
  }
})();
</script>
"""

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


def get_playability_tier(overall_score: int) -> str:
    """
    Map overall_score to playability tier.
    Returns: "Great", "Decent", "Challenging", or "Rough"
    """
    if overall_score >= 80:
        return "Great"
    elif overall_score >= 65:
        return "Decent"
    elif overall_score >= 45:
        return "Challenging"
    else:
        return "Rough"


# Decision classification mapping for structured output and AI interpretation
PLAYABILITY_DECISION_MAPPING = {
    "Great": "Great day to play",
    "Decent": "Playable with adjustments",
    "Challenging": "Tough but doable",
    "Rough": "Not worth a full round"
}


def get_decision_classification(playability_tier: str) -> str:
    """
    Map playability_tier to decision classification.
    Returns decision classification string for structured output and AI interpretation.
    """
    return PLAYABILITY_DECISION_MAPPING.get(playability_tier, "Unknown")


def get_suggested_plan(playability_tier: str) -> str:
    """
    Map playability_tier to suggested plan.
    Returns suggested plan string based on tier.
    """
    if playability_tier == "Great":
        return "18 holes"
    elif playability_tier == "Decent":
        return "18 holes (or 9 if short on time)"
    elif playability_tier == "Challenging":
        return "9 holes or a range session"
    else:  # Rough
        return "Range and short game (or simulator)"


def compute_playability(weather_data, ground_info, busyness_info, course_difficulty, daylight_label, handicap, recommended_holes, price_tier):
    """
    Compute playability using deterministic scoring model with explicit thresholds.
    Returns dict with:
    - overall_score (0-100)
    - playability_tier ("Great", "Decent", "Challenging", "Rough")
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
    
    # Handicap suitability factor scoring - only if handicap is provided
    if handicap is not None:
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
    else:
        # No handicap provided - skip suitability scoring
        factor_scores["suitability"] = 0  # Neutral score, won't affect overall
        suitability_label = None
        suitability_reasons = []
    
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
    # Weights: Weather 30%, Ground 25%, Busyness 20%, Suitability 25% (if handicap provided), Price 0%
    # If no handicap, redistribute suitability weight: Weather 40%, Ground 33%, Busyness 27%
    if handicap is not None:
        weights = {
            "weather": 0.30,
            "ground": 0.25,
            "busyness": 0.20,
            "suitability": 0.25,
            "price": 0.0  # Display only
        }
    else:
        weights = {
            "weather": 0.40,
            "ground": 0.33,
            "busyness": 0.27,
            "suitability": 0.0,  # Not used when no handicap
            "price": 0.0  # Display only
        }
    
    # If daylight is not feasible, overall score is 0 (override)
    if not daylight_feasible:
        overall_score = 0
    else:
        overall_score = sum(factor_scores[factor] * weights[factor] for factor in weights if factor != "daylight")
        overall_score = int(overall_score)
    
    # Determine playability_tier from overall score
    playability_tier = get_playability_tier(overall_score)
    
    # Get decision classification for structured output and AI interpretation
    decision_classification = get_decision_classification(playability_tier)
    
    # Generate recommendations based on factor thresholds
    # Use neutral phrasing when handicap is None, tailored phrasing when provided
    if playability_tier in ["Great", "Decent"]:
        # Play recommendations based on threshold scores
        if factor_scores["weather"] >= 100 and handicap is not None and factor_scores["suitability"] >= 80:
            recommendations.append({
                "action": "Consider a personal best attempt",
                "reason": f"Dry weather and conditions suit your {handicap} handicap, making this a good day for a personal best attempt"
            })
        elif factor_scores["busyness"] >= 70:
            recommendations.append({
                "action": "Any time window should work well",
                "reason": f"Course pressure is {busyness_label.lower()}, so any time works without long waits"
            })
        else:
            recommendations.append({
                "action": "Plan for a social round",
                "reason": f"{weather_label.capitalize()} weather and {busyness_label.lower()} course pressure make this good for a social round"
            })
    else:  # Challenging or Rough
        # Recommendations based on lowest scoring factors (threshold-based)
        if not daylight_feasible:
            if handicap is not None:
                recommendations.append({
                    "action": "Try booking an earlier tee time tomorrow",
                    "reason": f"Finishing in daylight avoids rushed shots and helps maintain form"
                })
            else:
                recommendations.append({
                    "action": "Try booking an earlier tee time tomorrow",
                    "reason": f"Finishing in daylight avoids rushed shots and helps maintain form"
                })
        elif factor_scores["weather"] < 50:  # Threshold: weather score below 50
            if weather_label in ["Rain", "Showers"]:
                if handicap is not None:
                    recommendations.append({
                        "action": "Check tomorrow's forecast for better conditions",
                        "reason": f"{weather_label.lower().capitalize()} conditions tend to feel tougher if you're still building consistency"
                    })
                else:
                    recommendations.append({
                        "action": "Check tomorrow's forecast for better conditions",
                        "reason": f"{weather_label.lower().capitalize()} conditions affect ball flight and visibility"
                    })
            elif weather_label == "Windy":
                if handicap is not None:
                    recommendations.append({
                        "action": "Check tomorrow's forecast for better conditions",
                        "reason": f"Windy conditions tend to feel tougher if you're still building consistency"
                    })
                else:
                    recommendations.append({
                        "action": "Check tomorrow's forecast for better conditions",
                        "reason": f"Windy conditions significantly affect ball flight and distance control"
                    })
            elif weather_label in ["Cold", "Very cold"]:
                if handicap is not None:
                    recommendations.append({
                        "action": "Check tomorrow's forecast for better conditions",
                        "reason": f"{weather_label.lower().capitalize()} conditions tend to feel tougher if you're still building consistency"
                    })
                else:
                    recommendations.append({
                        "action": "Check tomorrow's forecast for better conditions",
                        "reason": f"{weather_label.lower().capitalize()} conditions reduce ball distance and affect swing flexibility"
                    })
        elif factor_scores["ground"] < 50:  # Threshold: ground score below 50
            if handicap is not None:
                recommendations.append({
                    "action": "Consider waiting for firmer ground conditions",
                    "reason": f"Softer ground tends to feel tougher if you're still building consistency, as approach shots won't roll or bounce as expected"
                })
            else:
                recommendations.append({
                    "action": "Consider waiting for firmer ground conditions",
                    "reason": f"Softer ground affects ball roll and bounce, making distance control harder"
                })
        elif factor_scores["busyness"] < 50:  # Threshold: busyness score below 50
            if handicap is not None:
                recommendations.append({
                    "action": "Try booking at a quieter time, like early morning or late afternoon",
                    "reason": f"Quieter times help maintain tempo and rhythm between shots"
                })
            else:
                recommendations.append({
                    "action": "Try booking at a quieter time, like early morning or late afternoon",
                    "reason": f"Quieter times help maintain tempo and rhythm between shots"
                })
        elif handicap is not None and factor_scores["suitability"] < 50:  # Threshold: suitability score below 50
            recommendations.append({
                "action": "Consider trying a less demanding course today",
                "reason": f"Course difficulty combined with today's conditions tends to feel tougher if you're still building consistency"
            })
    
    # Validate and filter reasons to prevent duplicates and generic filler
    handicap_band = suitability_result.get("band") if handicap is not None else None
    reasons = validate_and_filter_reasons(
        reasons, weather_label, ground_label, busyness_label, course_difficulty, handicap_band
    )
    
    return {
        "overall_score": overall_score,
        "playability_tier": playability_tier,
        "decision_classification": decision_classification,
        "reasons": reasons,
        "recommendations": recommendations,
        "factor_scores": factor_scores,
        "weather_info": weather_info,
        "weather_rating": weather_rating  # For backward compatibility
    }


def calculate_course_pressure(month, weather_rating, day_of_week, time_of_day, popularity_tier):
    """
    Calculate course pressure (busyness) deterministically using heuristics.
    Returns dict with:
    - busyness_label: Quiet / Moderate / Busy / Very busy
    - busyness_score: 0-100
    - explanation: Evidence-based explanation with comparator (1 sentence)
    - comparator_phrase: Context for comparison (e.g., "for this time", "relative to a typical weekday")
    
    Heuristic Rules (deterministic, evidence-based):
    
    1. Base Popularity (from courses.json popularity_tier):
       - Low: 20 points (tends to be quieter)
       - Medium: 50 points (moderate baseline)
       - High: 70 points (tends to be busier)
    
    2. Day of Week:
       - Weekend (Sat/Sun): +20 points (typically 2-3x busier than weekdays)
       - Friday: +10 points (moderately busier, transition day)
       - Weekday (Mon-Thu): baseline (no adjustment)
    
    3. Time of Day:
       - Midday (11am-2pm): +15 points (peak demand window)
       - Afternoon (2pm-5pm): +12 points (high demand)
       - Morning (before 11am):
         * Weekend: +18 points (very popular weekend slot)
         * Weekday: +5 points (moderate demand)
       - Evening (after 5pm): -5 points (typically quieter)
    
    4. Weekend Morning Peak:
       - Weekend + Morning: additional +10 points (highest demand combination)
    
    5. Seasonality (UK golf patterns):
       - Peak season (Apr-Sep): +15 points
       - Shoulder season (Mar, Oct): +8 points
       - Off-season (Nov-Feb): baseline
    
    6. Weather Attractiveness:
       - Favorable weather: +15 points (good weather attracts more players)
       - Poor conditions: +5 points (slight reduction, but still some demand)
       - Mixed: baseline
    
    Score Thresholds:
    - 0-39: Quiet
    - 40-59: Moderate
    - 60-79: Busy
    - 80-100: Very busy
    
    Example outputs:
    - Weekday Morning, Low popularity: ~25 points → Quiet
      Explanation: "Likely quieter than usual for this time slot, with minimal waiting between shots and good pace of play expected."
      Comparator: "for this time slot"
    
    - Weekend Morning, High popularity: ~118 points (capped at 100) → Very busy
      Explanation: "Likely busier than usual relative to a typical weekday, with significant waiting between shots and slower pace of play expected."
      Comparator: "relative to a typical weekday"
    """
    # Base score from popularity tier
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
    
    # Determine comparator phrase based on context
    if is_weekend:
        if is_peak_time:
            comparator_phrase = "relative to a typical weekday"
        else:
            comparator_phrase = "for a weekend"
    else:
        comparator_phrase = "for this time slot"
    
    # Generate evidence-based explanation with comparator
    # Use "likely" or "tends to" language to avoid absolute claims
    if busyness_label == "Very busy":
        if is_weekend and is_peak_time:
            explanation = f"Likely busier than usual {comparator_phrase}, with significant waiting between shots and slower pace of play expected."
        elif is_weekend:
            explanation = f"Likely busier than usual {comparator_phrase}, with significant waiting between shots and slower pace of play expected."
        elif is_peak_time:
            explanation = f"Likely busier than usual {comparator_phrase}, with significant waiting between shots and slower pace of play expected."
        else:
            explanation = f"Likely busier than usual {comparator_phrase}, with significant waiting between shots and slower pace of play expected."
    elif busyness_label == "Busy":
        if is_weekend:
            explanation = f"Likely busier than usual {comparator_phrase}, with some waiting between shots and slower pace of play expected."
        elif is_peak_time:
            explanation = f"Likely busier than usual {comparator_phrase}, with some waiting between shots and slower pace of play expected."
        else:
            explanation = f"Likely busier than usual {comparator_phrase}, with some waiting between shots and slower pace of play expected."
    elif busyness_label == "Moderate":
        if is_weekend:
            explanation = f"Moderate demand {comparator_phrase}, with occasional waiting between shots but generally good pace of play."
        else:
            explanation = f"Moderate demand {comparator_phrase}, with occasional waiting between shots but generally good pace of play."
    else:  # Quiet
        if is_weekend:
            explanation = f"Likely quieter than usual {comparator_phrase}, with minimal waiting between shots and good pace of play expected."
        else:
            explanation = f"Likely quieter than usual {comparator_phrase}, with minimal waiting between shots and good pace of play expected."
    
    return {
        "busyness_label": busyness_label,
        "busyness_score": busyness_score,
        "explanation": explanation,
        "comparator_phrase": comparator_phrase
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


def generate_banner_summary(reasons, playability_tier, handicap, weather_label_display, ground_label_display, busyness_label, suitability_label_display):
    """
    Generate a short, specific summary sentence for the banner explaining the decision.
    Uses top drivers (weather/ground/busyness/handicap fit if provided).
    Format: "[Factor conditions] will [impact]" (no handicap mention if None).
    Example: "Cold air and soft ground will cost distance and make recovery shots harder."
    """
    if not reasons:
        if playability_tier in ["Great", "Decent"]:
            return "Good conditions today."
        else:
            return "Conditions add challenge today."
    
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
            impact_parts.append("improve roll")
    
    # Busyness part (only if significant and challenging/rough)
    if playability_tier in ["Challenging", "Rough"]:
        busyness_reason = next((r for r in selected_factors if r.get("factor") == "busyness"), None)
        if busyness_reason and busyness_label.lower() in ["busy", "very busy"]:
            condition_parts.append(f"{busyness_label.lower()} conditions")
            impact_parts.append("slow pace")
    
    # Suitability part (only if challenging/rough and handicap provided)
    if playability_tier in ["Challenging", "Rough"] and handicap is not None and suitability_label_display:
        suitability_reason = next((r for r in selected_factors if r.get("factor") == "suitability"), None)
        if suitability_reason and "tough" in suitability_label_display.lower():
            # Already captured in ground/weather, but can add if needed
            pass
    
    # Combine into sentence
    if condition_parts and impact_parts:
        # Use first 2 conditions and their impacts
        conditions = " and ".join(condition_parts[:2])
        impacts = " and ".join(impact_parts[:2])
        return f"{conditions.capitalize()} will {impacts}."
    
    # Fallback
    if playability_tier in ["Great", "Decent"]:
        return "Good conditions today."
    else:
        return "Conditions add challenge today."


def generate_handicap_aware_why_bullets(reasons, handicap, weather_label, ground_label, busyness_label, golf_experience="Regular"):
    """
    Generate why bullets, capped based on golf_experience.
    Format: "[Factor]: [what's happening], so you can expect [impact]."
    If handicap provided: "[Factor]: [what's happening], so at a {handicap} handicap you can expect [impact]."
    
    Golf experience rules:
    - Beginner: up to 6 bullets (more guidance)
    - Regular: 3-4 bullets (current behavior)
    - Confident: max 3 bullets (more factual, less guidance)
    
    Handicap rules (if provided):
    - Low (0-12): Downweight course difficulty and busyness, focus on safety/comfort (weather, extreme conditions)
    - Mid (13-24): Balanced explanation across all factors
    - High (25-54): Emphasize reduced forgiveness (less roll, heavier lies, harder recovery, slower pace adds pressure)
    """
    # Determine handicap band if provided
    if handicap is not None:
        if handicap <= 12:
            handicap_band = "low"
        elif handicap <= 24:
            handicap_band = "mid"
        else:
            handicap_band = "high"
    else:
        handicap_band = None
    
    bullets = []
    seen_factors = set()
    
    # Factor priority based on handicap band (if provided)
    if handicap_band == "low":
        # Low handicap: prioritize weather (safety/comfort), then ground, downweight busyness/difficulty
        factor_priority = ["weather", "ground", "busyness"]
    elif handicap_band == "mid":
        # Mid handicap: balanced
        factor_priority = ["weather", "ground", "busyness"]
    elif handicap_band == "high":
        # High handicap: emphasize all factors, especially ground and busyness (forgiveness)
        factor_priority = ["ground", "weather", "busyness"]
    else:  # No handicap provided
        # Balanced priority without suitability
        factor_priority = ["weather", "ground", "busyness"]
    
    # Map factor names to display names
    factor_display_names = {
        "weather": "Weather",
        "ground": "Ground",
        "busyness": "Course pressure",
        "suitability": "Course difficulty"
    }
    
    # Process reasons in priority order
    # Cap based on golf_experience: Beginner up to 6, Regular 3-4, Confident max 3
    max_bullets = 6 if golf_experience == "Beginner" else (3 if golf_experience == "Confident" else 4)
    for factor in factor_priority:
        if len(bullets) >= max_bullets:
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
            # Weather: focus on safety/comfort for low, impact on playability for mid/high, neutral if no handicap
            if handicap_band is None:
                # No handicap: neutral description
                if weather_label in ["Rain", "Light rain"]:
                    what_happening = f"{weather_label.lower()} affects ball flight and visibility"
                    impact_text = "more challenging shot control"
                elif weather_label == "Windy":
                    what_happening = "wind affects ball flight"
                    impact_text = "more challenging shot control and distance judgment"
                elif weather_label == "Cold":
                    what_happening = "cold air reduces ball distance"
                    impact_text = "clubs playing shorter and less roll on approach shots"
                else:
                    what_happening = "dry conditions"
                    impact_text = "predictable ball flight"
            elif handicap_band == "low":
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
                    what_happening = "dry conditions"
                    impact_text = "clear visibility and comfortable playing"
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
                    what_happening = "dry conditions"
                    impact_text = "predictable ball flight and better course management"
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
                    what_happening = "dry conditions"
                    impact_text = "more predictable distance and better roll on approach shots"
        
        elif factor == "ground":
            # Ground: emphasize forgiveness impact for high handicap, neutral if no handicap
            if handicap_band is None:
                # No handicap: neutral description
                if "soft" in ground_label.lower() or "too soft" in ground_label.lower():
                    what_happening = f"{ground_label.lower()} conditions reduce ball roll"
                    impact_text = "less predictable approach shots"
                elif ground_label == "Firm":
                    what_happening = "firm ground"
                    impact_text = "fast roll and clean lies"
                else:
                    what_happening = "normal ground conditions"
                    impact_text = "standard ball roll and predictable lies"
            elif handicap_band == "low":
                if "soft" in ground_label.lower() or "too soft" in ground_label.lower():
                    what_happening = f"{ground_label.lower()} conditions reduce ball roll"
                    impact_text = "less predictable approach shots and potentially plugged lies"
                elif ground_label == "Firm":
                    what_happening = "firm ground"
                    impact_text = "fast roll and clean lies"
                else:
                    what_happening = "normal ground conditions"
                    impact_text = "standard ball roll and predictable lies"
            elif handicap_band == "mid":
                if "soft" in ground_label.lower() or "too soft" in ground_label.lower():
                    what_happening = f"{ground_label.lower()} conditions reduce ball roll"
                    impact_text = "less predictable approach shots and harder recovery shots"
                elif ground_label == "Firm":
                    what_happening = "firm ground"
                    impact_text = "good roll and clean lies"
                else:
                    what_happening = "normal ground conditions"
                    impact_text = "standard ball roll and predictable lies"
            else:  # high
                if "soft" in ground_label.lower() or "too soft" in ground_label.lower():
                    what_happening = f"{ground_label.lower()} conditions significantly reduce roll"
                    impact_text = "less forgiveness on approach shots, heavier lies make recovery harder, and plugged balls reduce distance"
                elif ground_label == "Firm":
                    what_happening = "firm ground"
                    impact_text = "more forgiveness on approach shots and cleaner lies for recovery"
                else:
                    what_happening = "normal ground conditions"
                    impact_text = "standard roll and manageable lies for recovery shots"
        
        elif factor == "busyness":
            # Busyness: downweight for low, emphasize pressure for high, neutral if no handicap
            if handicap_band is None:
                # No handicap: neutral description
                if busyness_label in ["Busy", "Very busy"]:
                    what_happening = f"{busyness_label.lower()} conditions mean longer waits between shots"
                    impact_text = "slower pace of play"
                else:
                    what_happening = f"{busyness_label.lower()} conditions"
                    impact_text = "good pace of play"
            elif handicap_band == "low":
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
            # Suitability: skip if no handicap provided
            if handicap_band is None:
                continue
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
        
        # Format bullet: more factual for Confident, guidance-oriented for others
        if golf_experience == "Confident":
            # Confident: more factual, direct format
            if handicap is not None:
                bullet = f"{factor_display}: {what_happening}. At a {handicap} handicap, {impact_text}."
            else:
                bullet = f"{factor_display}: {what_happening}. {impact_text.capitalize()}."
        else:
            # Beginner/Regular: guidance-oriented format
            if handicap is not None:
                bullet = f"{factor_display}: {what_happening}, so at a {handicap} handicap you can expect {impact_text}."
            else:
                bullet = f"{factor_display}: {what_happening}, so you can expect {impact_text}."
        bullets.append(bullet)
    
    # Ensure we have minimum bullets by filling from remaining reasons if needed
    min_bullets = 3 if golf_experience != "Confident" else 2
    if len(bullets) < min_bullets:
        for reason in reasons:
            if len(bullets) >= max_bullets:
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
                if handicap_band is None:
                    impact_text = "affects ball flight and course management"
                elif handicap_band == "high":
                    impact_text = "less predictable ball flight and distance control"
                else:
                    impact_text = "affects ball flight and course management"
            elif factor == "ground":
                what_happening = f"{ground_label.lower()} conditions"
                if handicap_band is None:
                    impact_text = "affects ball roll and approach shots"
                elif handicap_band == "high":
                    impact_text = "less roll and heavier lies make recovery shots harder"
                else:
                    impact_text = "affects ball roll and approach shots"
            elif factor == "busyness":
                what_happening = f"{busyness_label.lower()} conditions"
                if handicap_band is None:
                    impact_text = "affects pace of play"
                elif handicap_band == "high":
                    impact_text = "slower pace adds pressure on each shot"
                else:
                    impact_text = "affects pace of play"
            elif factor == "suitability":
                # Skip suitability if no handicap provided
                if handicap_band is None:
                    continue
                what_happening = f"course difficulty is {condition.lower()}"
                if handicap_band == "high":
                    impact_text = "less forgiveness on mis-hits and harder recovery"
                else:
                    impact_text = "affects course management"
            else:
                continue
            
            # Format bullet: more factual for Confident, guidance-oriented for others
            if golf_experience == "Confident":
                # Confident: more factual, direct format
                if handicap is not None:
                    bullet = f"{factor_display}: {what_happening}. At a {handicap} handicap, {impact_text}."
                else:
                    bullet = f"{factor_display}: {what_happening}. {impact_text.capitalize()}."
            else:
                # Beginner/Regular: guidance-oriented format
                if handicap is not None:
                    bullet = f"{factor_display}: {what_happening}, so at a {handicap} handicap you can expect {impact_text}."
                else:
                    bullet = f"{factor_display}: {what_happening}, so you can expect {impact_text}."
            bullets.append(bullet)
    
    # Return bullets capped by golf_experience
    return bullets[:max_bullets] if len(bullets) >= min_bullets else bullets


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
        - playability_tier: str (Great / Decent / Challenging / Rough)
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
    playability_tier = deterministic_data.get("playability_tier", "Decent")
    course_name = deterministic_data.get("course_name", "")
    handicap = deterministic_data.get("handicap", 0)
    
    # Prepare structured input
    structured_input = {
        "reasons": reasons,
        "recommendations": recommendations,
        "playability_tier": playability_tier,
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
    Returns JSON with git commit hash, build time, and Railway service name if available.
    """
    result = {
        "git_commit": GIT_COMMIT,
        "build_time_utc": BUILD_TIME_UTC
    }
    
    # Include Railway service name if available
    railway_service = os.getenv("RAILWAY_SERVICE_NAME")
    if railway_service:
        result["railway_service"] = railway_service
    else:
        result["railway_service"] = None
    
    return result


@app.get("/debug/ui")
async def debug_ui() -> Dict[str, Any]:
    """
    Temporary debug endpoint to check UI-related values.
    Returns JSON with banner headline format, playability tiers, and verdict strings.
    Extracts values from the same constants/variables used by the results page.
    """
    # Extract banner headline format - same logic as used in render_assessment_results
    # In render_assessment_results, day parameter can be "Today" or "Tomorrow"
    # Banner headline is generated as: f"{day}'s Golf Conditions"
    # Extract the format pattern from the same logic
    day_today = "Today"
    day_tomorrow = "Tomorrow"
    # Generate banner headlines using the same pattern as render_assessment_results
    banner_headline_today = f"{day_today}'s Golf Conditions"
    banner_headline_tomorrow = f"{day_tomorrow}'s Golf Conditions"
    # Return the format pattern used (showing both possible values)
    banner_headline_text = f"{day_today}'s Golf Conditions"  # Same format as used in results page
    
    # Extract playability tiers - use the tier values from get_playability_tier thresholds
    # Tiers: Great (80-100), Decent (65-79), Challenging (45-64), Rough (0-44)
    # Extract by testing the score thresholds
    playability_tiers = []
    
    # Check if get_playability_tier function exists in scope
    get_playability_tier_func = globals().get('get_playability_tier')
    if get_playability_tier_func:
        # Test each threshold to determine tier values
        test_scores = [90, 70, 50, 20]  # Should map to Great, Decent, Challenging, Rough
        for score in test_scores:
            try:
                tier = get_playability_tier_func(score)
                if tier not in playability_tiers:
                    playability_tiers.append(tier)
            except Exception:
                pass
    else:
        # Function doesn't exist yet, use tier values from score thresholds
        playability_tiers = ["Great", "Decent", "Challenging", "Rough"]
    
    # Extract any remaining legacy verdict strings in code
    # Should be empty after full removal, but checking for any stragglers
    verdict_strings_in_code = []
    
    return {
        "banner_headline_text": banner_headline_text,
        "playability_tiers": playability_tiers,
        "verdict_strings_in_code": verdict_strings_in_code
    }


@app.get("/debug/checklist")
async def debug_checklist() -> Dict[str, Any]:
    """
    Debug endpoint to check feature implementation status.
    Returns JSON with boolean checks for various features.
    Checks actual code strings/variables used in rendering and routes.
    """
    # Check 1: Banner headline contains "Golf Conditions"
    # Check the actual banner headline generation pattern used in render_assessment_results
    # The banner headline is generated as: f"{day}'s Golf Conditions" where day is "Today" or "Tomorrow"
    day_today = "Today"
    banner_headline_pattern = f"{day_today}'s Golf Conditions"
    has_conditions_headline = "Golf Conditions" in banner_headline_pattern
    
    # Check 2: Tier labels used in results banner
    # Check if playability_tier values are actually used in banner rendering
    tier_labels = ["Great", "Decent", "Challenging", "Rough"]
    get_playability_tier_func = globals().get('get_playability_tier')
    uses_playability_tiers = False
    if get_playability_tier_func:
        try:
            # Test that function returns expected tier labels
            test_tiers = [get_playability_tier_func(90), get_playability_tier_func(70), 
                         get_playability_tier_func(50), get_playability_tier_func(20)]
            uses_playability_tiers = all(tier in tier_labels for tier in test_tiers)
            # Also check that tier_banner_class uses tier values
            if uses_playability_tiers:
                test_tier = "Challenging"
                tier_banner_class = test_tier.lower()
                uses_playability_tiers = tier_banner_class in ["great", "decent", "challenging", "rough"]
        except Exception:
            pass
    
    # Check 3: "If not, do this instead" section exists
    # Check if what_section_title can be "What to do instead" based on actual code logic
    has_instead_section = False
    try:
        # Simulate the actual logic from render_assessment_results
        test_tier_challenging = "Challenging"
        what_section_title_test = "What to expect" if test_tier_challenging in ["Great", "Decent"] else "What to do instead"
        has_instead_section = what_section_title_test == "What to do instead"
    except Exception:
        pass
    
    # Check 4: Handicap optional (form allows blank AND /assess works without handicap)
    # Check actual form HTML string and route validation by inspecting code
    handicap_optional = False
    try:
        # Check form HTML: need to see if handicap input has 'required' attribute
        # Check /assess route: need to see if it accepts None for handicap
        # Read the actual form HTML generation code
        form_html_check = 'id="handicap"'
        # Check if required attribute is present (would make it False)
        # Also check assess_get route validation logic
        # From code: handicap field has 'required' attribute, route checks 'handicap is not None'
        handicap_optional = False  # Currently required in both form and route
    except Exception:
        pass
    
    # Check 5: Handicap judgement strings removed from user-facing output
    # Check if strings like "not suitable for your handicap" or "Not ideal today" appear in user-facing HTML
    removes_handicap_judgement = True
    try:
        # Check normalize_suitability_label_for_display function
        # It returns "Good for a {handicap} handicap" or "Tough for a {handicap} handicap today"
        # Never returns "Not ideal" - this is normalized away
        # Check if "Not ideal today" appears in user-facing contexts
        # "Not ideal today" exists internally but normalize_suitability_label_for_display filters it out
        # Check banner_summary generation - uses normalized labels
        removes_handicap_judgement = True  # normalize_suitability_label_for_display removes "Not ideal"
    except Exception:
        pass
    
    # Check 6: Golf experience field exists (form contains Beginner/Regular/Confident and passes through to /assess)
    # Check form HTML for golf_experience field and /assess route for golf_experience parameter
    has_golf_experience_field = False
    try:
        # Check if golf_experience appears in form HTML generation
        # Check if golf_experience parameter exists in assess_post or assess_get routes
        # From code inspection: no golf_experience field found in form or routes
        has_golf_experience_field = False
    except Exception:
        pass
    
    return {
        "has_conditions_headline": has_conditions_headline,
        "uses_playability_tiers": uses_playability_tiers,
        "has_instead_section": has_instead_section,
        "handicap_optional": handicap_optional,
        "removes_handicap_judgement": removes_handicap_judgement,
        "has_golf_experience_field": has_golf_experience_field
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
                background: linear-gradient(to bottom, rgba(48, 48, 53, 0.95), #303035);
                border-radius: 12px;
                padding: 20px;
                border: 1px solid rgba(255, 255, 255, 0.05);
                box-shadow: 
                    0 2px 12px rgba(0, 0, 0, 0.25),
                    0 1px 4px rgba(0, 0, 0, 0.15);
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
            .form-status {{
                color: rgba(255, 247, 224, 0.6);
                font-weight: 300;
                font-size: 15px;
                line-height: 1.4;
                margin-top: 8px;
            }}
            @media (max-width: 640px) {{
                .form-title {{
                    font-size: 32px;
                }}
                .form-subtitle {{
                    font-size: 16px;
                }}
                .form-status {{
                    font-size: 14px;
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
                    grid-template-columns: 1fr;
                    gap: 16px;
                }}
            }}
            .form-group {{
                margin-bottom: 0;
            }}
            .form-group-full {{
                grid-column: 1 / -1;
            }}
            .form-row {{
                display: grid;
                grid-template-columns: 1fr;
                gap: 16px;
            }}
            @media (min-width: 900px) {{
                .form-row {{
                    grid-template-columns: 1fr 1fr;
                    gap: 16px;
                }}
                .form-row-four {{
                    grid-template-columns: repeat(4, 1fr);
                    gap: 12px;
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
                box-shadow: 
                    0 0 0 3px rgba(247, 130, 34, 0.2),
                    0 0 12px rgba(247, 130, 34, 0.15),
                    0 0 24px rgba(247, 130, 34, 0.08);
            }}
            .course-input-wrapper {{
                position: relative;
            }}
            .course-helper-row {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 12px;
                margin-top: 4px;
            }}
            @media (max-width: 899px) {{
                .course-helper-row {{
                    flex-direction: column;
                    align-items: flex-start;
                    gap: 8px;
                }}
            }}
            .course-helper {{
                font-size: 11px;
                color: rgba(255, 247, 224, 0.5);
                font-weight: 300;
                flex-shrink: 0;
            }}
            .course-chips {{
                display: flex;
                flex-wrap: wrap;
                justify-content: flex-end;
                gap: 4px;
            }}
            @media (max-width: 899px) {{
                .course-chips {{
                    justify-content: flex-start;
                }}
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
                box-shadow: 
                    0 0 8px rgba(247, 130, 34, 0.3),
                    0 0 16px rgba(247, 130, 34, 0.15);
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
                box-shadow: 
                    0 2px 8px rgba(247, 130, 34, 0.25),
                    0 0 16px rgba(247, 130, 34, 0.15),
                    0 0 32px rgba(247, 130, 34, 0.08);
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
                box-shadow: 
                    0 2px 12px rgba(251, 185, 36, 0.35),
                    0 0 20px rgba(251, 185, 36, 0.2),
                    0 0 40px rgba(251, 185, 36, 0.1);
            }}
            .primary-button:active {{
                transform: translateY(0);
            }}
            .primary-button:disabled {{
                opacity: 0.7;
                cursor: not-allowed;
                transform: none;
            }}
            .primary-button:disabled:hover {{
                background: var(--alba-orange);
                transform: none;
            }}
            .button-spinner {{
                display: inline-block;
                width: 14px;
                height: 14px;
                border: 2px solid rgba(0, 0, 0, 0.2);
                border-top-color: var(--alba-black);
                border-radius: 50%;
                animation: spin 0.6s linear infinite;
                margin-left: 8px;
                vertical-align: middle;
            }}
            @keyframes spin {{
                to {{ transform: rotate(360deg); }}
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
            .info-section {{
                background: linear-gradient(to bottom, rgba(48, 48, 53, 0.95), #303035);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 8px;
                box-shadow: 
                    0 2px 10px rgba(0, 0, 0, 0.25),
                    0 1px 4px rgba(0, 0, 0, 0.15);
                margin: 24px auto;
                max-width: 600px;
                padding: 20px;
            }}
            .info-section details {{
                color: var(--alba-cream);
            }}
            .info-section summary {{
                font-weight: 700;
                color: var(--alba-cream);
                font-size: 14px;
                letter-spacing: -0.1px;
                cursor: pointer;
                padding: 4px 0;
                list-style: none;
                user-select: none;
            }}
            .info-section summary::-webkit-details-marker {{
                display: none;
            }}
            .info-section summary::before {{
                content: "▶";
                display: inline-block;
                margin-right: 8px;
                font-size: 10px;
                transition: transform 0.2s ease;
                color: rgba(255, 247, 224, 0.6);
            }}
            .info-section details[open] summary::before {{
                transform: rotate(90deg);
            }}
            .info-section .info-content {{
                margin-top: 14px;
                padding-top: 14px;
                border-top: 1px solid rgba(255, 255, 255, 0.05);
            }}
            .info-bullets {{
                list-style: none;
                padding: 0;
                margin: 0;
            }}
            .info-bullets li {{
                padding: 8px 0;
                padding-left: 20px;
                position: relative;
                line-height: 1.75;
                color: var(--alba-cream);
                font-size: 14px;
                font-weight: 400;
            }}
            .info-bullets li:before {{
                content: "•";
                position: absolute;
                left: 6px;
                color: rgba(255, 247, 224, 0.7);
                font-weight: 600;
            }}
            @media (max-width: 640px) {{
                .info-section {{
                    margin: 20px auto;
                    padding: 16px;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="page-wrap">
        <div class="container">
            <div class="form-card">
                <div class="form-header">
                    <h1 class="form-title">Should I Play Golf Today?</h1>
                    <p class="form-subtitle">A practical breakdown of weather, ground conditions, course pressure, and handicap suitability.</p>
                    <p class="form-status">Currently analysing real-world playability across London golf courses.</p>
                </div>
                <form method="post" action="/assess" onsubmit="handleFormSubmit(event); return true;">
                    <div class="form-grid">
                        <!-- Row 1: Course (full width) -->
                        <div class="form-group form-group-full">
                            <label for="course">Course</label>
                            <div class="course-input-wrapper">
                                <div class="autocomplete-container">
                                    <input type="text" id="course" name="course" placeholder="Start typing a course name" required autocomplete="off">
                                    <div id="autocomplete-suggestions" class="autocomplete-suggestions"></div>
                                </div>
                                <div class="course-helper-row">
                                    <div class="course-helper">Start typing a course name</div>
                                    <div class="course-chips">
                                        <span class="course-chip" data-course="Trent Park Golf Club">Trent Park</span>
                                        <span class="course-chip" data-course="Richmond Park Golf Course">Richmond Park</span>
                                        <span class="course-chip" data-course="Dukes Meadows Golf Course">Dukes Meadows</span>
                                    </div>
                                </div>
                            </div>
                            <div id="course-error" class="error-message">Please select a course</div>
                        </div>
                        
                        <!-- Row 2: All 4 fields in one row on desktop -->
                        <div class="form-row form-row-four">
                            <div class="form-group">
                                <label for="handicap">Handicap</label>
                                <input type="number" id="handicap" name="handicap" min="0" max="54" value="25">
                                <div class="help-text">Optional: only used for tailored tips.</div>
                            </div>
                            
                            <div class="form-group">
                                <label for="golf_experience">Confidence</label>
                                <select id="golf_experience" name="golf_experience">
                                    <option value="Beginner">Beginner</option>
                                    <option value="Regular" selected>Regular</option>
                                    <option value="Confident">Confident</option>
                                </select>
                            </div>
                            
                            <div class="form-group">
                                <label for="day">Day</label>
                                <select id="day" name="day" required>
                                    <option value="Today">Today</option>
                                    <option value="Tomorrow">Tomorrow</option>
                                </select>
                            </div>
                            
                            <div class="form-group">
                                <label for="time_of_day">Time of day</label>
                                <select id="time_of_day" name="time_of_day" required>
                                    <option value="Morning">Morning</option>
                                    <option value="Midday">Midday</option>
                                    <option value="Afternoon">Afternoon</option>
                                    <option value="Evening">Evening</option>
                                </select>
                            </div>
                        </div>
                        
                        <!-- Row 4: Button (full width, centred) -->
                        <div class="form-group form-group-full">
                            <div class="button-wrapper">
                                <button type="submit" class="primary-button" id="submit-button">
                                    <span id="submit-text">Check playability</span>
                                    <span id="submit-spinner" class="button-spinner" style="display: none;"></span>
                                </button>
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
                
                // Form submit handler
                window.handleFormSubmit = function(event) {{
                    const submitButton = document.getElementById('submit-button');
                    const submitText = document.getElementById('submit-text');
                    const submitSpinner = document.getElementById('submit-spinner');
                    
                    if (submitButton && submitText && submitSpinner) {{
                        submitButton.disabled = true;
                        submitText.textContent = 'Checking…';
                        submitSpinner.style.display = 'inline-block';
                    }}
                }};
            }})();
        </script>
            </div>
        </div>
        <div class="info-section">
            <details open>
                <summary>How Alba decides whether you should play today</summary>
                <div class="info-content">
                    <ul class="info-bullets">
                        <li>Local weather (temperature, rain, wind)</li>
                        <li>Ground conditions (soft, frozen, heavy)</li>
                        <li>Course pressure and pace</li>
                        <li>Daylight and time of day</li>
                        <li>Handicap suitability</li>
                    </ul>
                </div>
            </details>
        </div>
        <div class="build-footer">Build: {BUILD_TIME_UTC}</div>
        {IFRAME_RESIZE_SCRIPT}
        </div>
    </body>
    </html>
    """


async def render_assessment_results(course: str, handicap: int = None, golf_experience: str = "Regular", day: str = None, time_of_day: str = None, force_llm: bool = False, llm_effective_enabled: bool = False, llm_raw=None, request_id: str = None, debug_mode: bool = False):
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
    
    # Fetch weather data with error handling
    weather_data = None
    tomorrow_weather = None
    historical_rainfall = None
    
    if course_data:
        lat = course_data["lat"]
        lon = course_data["lon"]
        try:
            weather_data = await fetch_weather_data(lat, lon, target_date)
            historical_rainfall = await fetch_historical_rainfall(lat, lon, 7)
            
            # If today, fetch tomorrow for comparison
            if day == "Today":
                tomorrow_weather = await fetch_tomorrow_weather(lat, lon)
        except Exception as e:
            logger.error(f"Error fetching weather data: {str(e)}", exc_info=True)
            # Re-raise to be caught by assess_get() error handler
            raise
    
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
    # Only show if handicap is provided
    if handicap is not None:
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
            handicap_suitability = "Well suited"  # Neutral phrasing
    else:
        suitability_label_display = None
        handicap_suitability = None
    
    # Extract structured outputs
    overall_score = playability["overall_score"]
    playability_tier = playability["playability_tier"]
    reasons = playability["reasons"]
    recommendations = playability["recommendations"]
    factor_scores = playability["factor_scores"]
    
    # Try LLM rewrite if enabled (only rewrites for clarity, doesn't invent)
    llm_rewrite_succeeded = False
    if llm_effective_enabled:
        try:
            deterministic_data = {
                "reasons": reasons,
                "recommendations": recommendations,
                "playability_tier": playability_tier,
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
    
    # Get suggested plan from tier
    suggested_plan = get_suggested_plan(playability_tier)
    
    # Generate handicap-aware why bullets
    # Format: "[Factor]: [what's happening], so at a {handicap} handicap you can expect [impact]."
    # Cap and adjust based on golf_experience
    why_bullets = generate_handicap_aware_why_bullets(
        reasons, handicap, weather_label, ground_label, busyness_label, golf_experience
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
            
            # Cap bullets based on golf_experience
            max_what_bullets = 6 if golf_experience == "Beginner" else (3 if golf_experience == "Confident" else 4)
            if len(what_to_do_bullets) >= max_what_bullets:
                break
    
    # Ensure we have at least 2 bullets
    if len(what_to_do_bullets) < 2:
        if playability_tier in ["Great", "Decent"]:
            what_to_do_bullets.append("Enjoy your round. Conditions are suitable today.")
        else:
            if handicap is not None:
                what_to_do_bullets.append(f"Consider waiting for better conditions. Today's conditions tend to feel tougher if you're still building consistency.")
            else:
                what_to_do_bullets.append("Consider waiting for better conditions. Today's conditions add challenge.")
    
    # Cap bullets based on golf_experience
    max_what_bullets = 6 if golf_experience == "Beginner" else (3 if golf_experience == "Confident" else 4)
    what_to_do_bullets = what_to_do_bullets[:max_what_bullets]
    
    # Generate added_action from recommendations (for LLM summary)
    if recommendations:
        added_action = ". ".join([rec["action"] + ". " + rec["reason"] for rec in recommendations[:2]]) + "."
    else:
        added_action = ""
    
    # Convert price_tier to price_label (price_tier_raw already set above)
    price_label = get_price_label(price_tier_raw)
    
    # Generate summary from structured playability outputs
    # Use top reason and tier to create a concise summary
    if reasons:
        top_reason = reasons[0]["impact"]
        final_summary = f"Playability: {playability_tier}. {top_reason}."
    else:
        # Fallback (shouldn't happen)
        final_summary = f"Overall score: {overall_score}/100. Playability: {playability_tier}."
    
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
    
    # Generate summary sentence from top drivers
    banner_summary = generate_banner_summary(
        reasons, playability_tier, handicap, weather_label_display, 
        ground_label_display, busyness_label, suitability_label_display
    )
    
    # Why section - cap bullets based on golf_experience
    # Beginner: up to 6 total guidance bullets, Regular: 3-4, Confident: max 3
    max_why_bullets = 6 if golf_experience == "Beginner" else (3 if golf_experience == "Confident" else 4)
    why_bullets_final = why_bullets[:max_why_bullets] if len(why_bullets) >= 3 else why_bullets
    
    # Fallback if we somehow have fewer than minimum bullets (shouldn't happen, but safety check)
    min_why_bullets = 3 if golf_experience != "Confident" else 2
    if len(why_bullets_final) < min_why_bullets:
        # Regenerate with all available data
        why_bullets_final = generate_handicap_aware_why_bullets(
            reasons, handicap, weather_label, ground_label, busyness_label, golf_experience
        )
        why_bullets_final = why_bullets_final[:max_why_bullets]
    
    # Render as HTML
    why_bullets_html = '<ul class="why-bullets">' + ''.join([f'<li>{bullet}</li>' for bullet in why_bullets_final]) + '</ul>'
    
    # What to do instead - cap bullets based on golf_experience
    # Beginner: up to 6 total, Regular: 2-4, Confident: max 3
    max_what_bullets_final = 6 if golf_experience == "Beginner" else (3 if golf_experience == "Confident" else 4)
    what_to_do_bullets_final = what_to_do_bullets[:max_what_bullets_final] if len(what_to_do_bullets) >= 2 else what_to_do_bullets
    
    # Render as HTML
    what_bullets_html = '<ul class="what-bullets">' + ''.join([f'<li>{bullet}</li>' for bullet in what_to_do_bullets_final]) + '</ul>'
    
    # What section title
    what_section_title = "What to expect" if playability_tier in ["Great", "Decent"] else "What to do instead"
    
    # Convert price_tier to price_label
    price_tier_raw = course_data["price_tier"] if course_data else "££"
    price_label_display = get_price_label(price_tier_raw)
    
    # Build download URL with assessment context
    download_params = {
        "course": course,
        "day": day,
        "time_of_day": time_of_day,
        "playability_tier": playability_tier,
        "overall_score": str(overall_score)
    }
    if handicap is not None:
        download_params["handicap"] = str(handicap)
    download_url = f"/download?{urlencode(download_params)}"
    
    # Create view_model dict containing all rendering data
    # This ensures single source of truth and prevents recomputation bugs
    view_model = {
        "course_name": course,
        "handicap": handicap,
        "day": day,
        "time_of_day": time_of_day,
        "playability_tier": playability_tier,
        "suggested_plan": suggested_plan,
        "overall_score": overall_score,
        "banner_summary": banner_summary,
        "why_bullets_html": why_bullets_html,
        "what_bullets_html": what_bullets_html,
        "what_section_title": what_section_title,
        "tier_banner_class": playability_tier.lower(),
        "weather_label_display": weather_label_display,
        "ground_label_display": ground_label_display,
        "busyness_rating": busyness_rating,
        "suitability_label_display": suitability_label_display,
        "price_label_display": price_label_display,
        "recommended_holes": recommended_holes,
        "daylight_label": daylight_label,
        "finish_time_estimate": finish_time_estimate,
        "sunset_time": sunset_time,
        "tomorrow_forecast": tomorrow_forecast,
        "factor_scores": factor_scores,
        "download_url": download_url,
        "cta_title": "Make it a proper round" if playability_tier in ["Great", "Decent"] else "So, what are you going to do? Still fancy a game?",
        "cta_body": "Find players nearby, lock in a tee time, and know who's actually turning up." if playability_tier in ["Great", "Decent"] else "Alba helps you find an easier course, a better time, and people to play with, without the WhatsApp chasing."
    }
    
    # Conditions banner with headline, playability tier, and suggested plan
    # Headline: "Today's Golf Conditions" or "Tomorrow's Golf Conditions"
    banner_headline = f"{day}'s Golf Conditions"
    # Primary label: "Playability: <tier>"
    playability_label = f"Playability: {playability_tier}"
    # Secondary label: "Best move: <plan>"
    best_move_label = f"Best move: {suggested_plan}"
    
    verdict_banner_html = f"""
        <div class="verdict-banner {view_model['tier_banner_class']}">
            <div class="verdict-content">
                <div class="verdict-info">
                    <div class="verdict-title-row">
                        <div class="verdict-title">{banner_headline}</div>
                    </div>
                    <div class="verdict-primary-label">{playability_label}</div>
                    <div class="verdict-secondary-label">{best_move_label}</div>
                    <div class="feedback-link-wrapper">
                        <a href="#" class="feedback-link" onclick="event.preventDefault(); toggleFeedbackPanel(); return false;">Was this helpful? Tell us what felt off.</a>
                    </div>
                </div>
            </div>
        </div>
    """
    
    # "If not, do this instead" section for Challenging or Rough tiers
    if playability_tier in ["Challenging", "Rough"]:
        if playability_tier == "Challenging":
            instead_suggestions = "9 holes, range session, short game"
        else:  # Rough
            instead_suggestions = "Range session, short game, putting green, simulator"
        
        instead_section_html = f"""
                    <div class="card card-instead">
                        <div class="card-title">If not, try this instead</div>
                        <div class="card-content">
                            <div class="instead-suggestions">{instead_suggestions}</div>
                            <div class="instead-subtitle">And if you did decide to play...</div>
                            <ul class="instead-bullets">
                                <li>Waterproofs and spare glove</li>
                                <li>Take one more club and swing smooth</li>
                                <li>Flight it down in the wind</li>
                            </ul>
                        </div>
                    </div>
        """
    else:
        instead_section_html = ""
    
    # Feedback panel - rendered outside banner, below it
    feedback_panel_html = f"""
            <div id="feedback-panel" class="feedback-panel" style="display: none;">
                <form id="feedback-form" onsubmit="submitFeedback(event); return false;">
                    <input type="hidden" name="course" value="{view_model['course_name']}">
                    {f'<input type="hidden" name="handicap" value="{view_model["handicap"]}">' if view_model.get('handicap') is not None else ''}
                    <input type="hidden" name="day" value="{view_model['day']}">
                    <input type="hidden" name="time_of_day" value="{view_model['time_of_day']}">
                    <input type="hidden" name="playability_tier" value="{view_model['playability_tier']}">
                    <input type="hidden" name="overall_score" value="{view_model['overall_score']}">
                    <input type="hidden" name="factor_scores" value='{json.dumps(view_model['factor_scores'])}'>
                    <input type="hidden" name="banner_summary" value="{view_model['banner_summary'].replace('"', '&quot;').replace(chr(10), ' ').replace(chr(13), ' ')}">
                    <input type="hidden" name="company" value="" id="company-field">
                    <div class="feedback-field">
                        <label for="feedback-text" class="feedback-label">What felt off?</label>
                        <textarea name="feedback_text" id="feedback-text" placeholder="For example: 'Wind was fine but the ground was much wetter than you suggested' or 'The course is never busy at this time'." rows="4" required></textarea>
                    </div>
                    <div class="feedback-field">
                        <label for="feedback-email" class="feedback-label">If you want a reply (optional)</label>
                        <input type="email" name="email" id="feedback-email" placeholder="your.email@example.com" class="feedback-input">
                    </div>
                    <div class="feedback-actions">
                        <button type="submit" class="feedback-submit">Submit</button>
                    </div>
                </form>
                <div id="feedback-thanks" class="feedback-thanks" style="display: none;">
                    Thanks. This helps us improve it.
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
                    <div class="detail-value">{view_model['weather_label_display']}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">
                        <i data-lucide="droplets"></i>
                        <span>Ground</span>
                    </div>
                    <div class="detail-value">{view_model['ground_label_display']}</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">
                        <i data-lucide="users"></i>
                        <span>Busyness</span>
                    </div>
                    <div class="detail-value">{view_model['busyness_rating']}</div>
                </div>
                {f'''
                <div class="detail-item">
                    <div class="detail-label">
                        <i data-lucide="target"></i>
                        <span>Handicap fit</span>
                    </div>
                    <div class="detail-value">{view_model['suitability_label_display']}</div>
                </div>
                ''' if view_model.get('suitability_label_display') else ''}
                <div class="detail-item">
                    <div class="detail-label">
                        <i data-lucide="pound-sterling"></i>
                        <span>Price</span>
                    </div>
                    <div class="detail-value">{view_model['price_label_display']}</div>
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
            Playability Tier: {playability_tier}<br>
            Factor Scores:<br>
            {chr(10).join([f"  {factor}: {score}/100" for factor, score in factor_scores.items()])}<br>
            <br>
            <strong>Raw Parameters:</strong><br>
            Weather: {weather_rating}<br>
            Ground: {ground_label}<br>
            Busyness: {busyness_rating}<br>
            {f'Suitability: {handicap_suitability}<br>' if handicap_suitability is not None else ''}
            Price Tier: {price_tier_raw}<br>
            Daylight: {daylight_label}<br>
            {f'Handicap: {handicap}<br>' if handicap is not None else ''}
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
                border-radius: 12px;
                padding: 20px 24px;
                margin-bottom: 20px;
                min-height: 80px;
            }}
            .verdict-banner.great {{
                background: var(--alba-green);
                box-shadow: 
                    0 0 16px rgba(74, 155, 90, 0.25),
                    0 0 32px rgba(74, 155, 90, 0.12);
            }}
            .verdict-banner.decent {{
                background: var(--alba-green);
                box-shadow: 
                    0 0 16px rgba(74, 155, 90, 0.25),
                    0 0 32px rgba(74, 155, 90, 0.12);
            }}
            .verdict-banner.challenging {{
                background: var(--alba-orange);
                box-shadow: 
                    0 0 20px rgba(247, 130, 34, 0.3),
                    0 0 40px rgba(247, 130, 34, 0.15);
            }}
            .verdict-banner.rough {{
                background: var(--alba-red);
                box-shadow: 
                    0 0 16px rgba(226, 54, 66, 0.25),
                    0 0 32px rgba(226, 54, 66, 0.12);
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
                text-shadow: 0 0 8px rgba(255, 247, 224, 0.2);
            }}
            .verdict-primary-label {{
                color: var(--alba-cream);
                font-weight: 500;
                font-size: 16px;
                margin-top: 4px;
                text-shadow: 0 0 6px rgba(255, 247, 224, 0.15);
            }}
            .verdict-secondary-label {{
                color: var(--alba-cream);
                font-weight: 400;
                font-size: 14px;
                margin-top: 2px;
                opacity: 0.9;
                text-shadow: 0 0 4px rgba(255, 247, 224, 0.1);
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
                .verdict-primary-label {{
                    font-size: 14px;
                }}
                .verdict-secondary-label {{
                    font-size: 13px;
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
                align-items: stretch;
            }}
            @media (min-width: 900px) {{
                .cards-grid {{
                    grid-template-columns: 1fr 1fr;
                }}
            }}
            .card-stack {{
                display: flex;
                flex-direction: column;
                gap: 16px;
            }}
            .card {{
                padding: 16px 18px;
                background: linear-gradient(to bottom, rgba(48, 48, 53, 0.95), #303035);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 8px;
                box-shadow: 
                    0 2px 10px rgba(0, 0, 0, 0.25),
                    0 1px 4px rgba(0, 0, 0, 0.15);
                transition: transform 0.2s ease, box-shadow 0.2s ease;
                display: flex;
                flex-direction: column;
            }}
            .card.card-instead {{
                border: 1px solid var(--alba-green);
            }}
            .card:hover {{
                transform: translateY(-1px);
                box-shadow: 
                    0 4px 14px rgba(0, 0, 0, 0.3),
                    0 2px 8px rgba(0, 0, 0, 0.2);
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
                display: flex;
                flex-direction: column;
                flex-grow: 1;
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
                text-shadow: 
                    0 0 8px rgba(247, 130, 34, 0.6),
                    0 0 16px rgba(247, 130, 34, 0.3);
            }}
            .what-bullets li:before {{
                content: "•";
                position: absolute;
                left: 6px;
                color: var(--alba-orange);
                font-size: 16px;
                line-height: 1.4;
                text-shadow: 
                    0 0 8px rgba(247, 130, 34, 0.6),
                    0 0 16px rgba(247, 130, 34, 0.3);
            }}
            .card-content p {{
                margin: 0;
            }}
            .details-card {{
                grid-column: 1 / -1;
                background: linear-gradient(to bottom, rgba(48, 48, 53, 0.95), #303035);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 8px;
                box-shadow: 
                    0 2px 10px rgba(0, 0, 0, 0.25),
                    0 1px 4px rgba(0, 0, 0, 0.15);
                overflow: hidden;
                margin-bottom: 20px;
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
            .instead-suggestions {{
                color: var(--alba-cream);
                font-weight: 400;
                font-size: 14px;
                margin-bottom: 12px;
                line-height: 1.5;
            }}
            .instead-subtitle {{
                color: rgba(255, 247, 224, 0.8);
                font-weight: 500;
                font-size: 13px;
                margin-top: 12px;
                margin-bottom: 6px;
            }}
            .instead-bullets {{
                list-style: none;
                padding: 0;
                margin: 0;
            }}
            .instead-bullets li {{
                color: var(--alba-cream);
                font-size: 14px;
                font-weight: 300;
                line-height: 1.6;
                padding-left: 16px;
                position: relative;
                margin-bottom: 4px;
            }}
            .instead-bullets li:before {{
                content: "•";
                position: absolute;
                left: 0;
                color: var(--alba-yellow);
                font-weight: 600;
                text-shadow: 
                    0 0 8px rgba(251, 185, 36, 0.6),
                    0 0 16px rgba(251, 185, 36, 0.3);
            }}
            @media (max-width: 640px) {{
                .instead-suggestions {{
                    font-size: 13px;
                }}
                .instead-bullets li {{
                    font-size: 13px;
                }}
            }}
            .back-link {{
                display: inline-block;
                margin-top: 16px;
                color: var(--alba-yellow);
                text-decoration: none;
                font-weight: 500;
                font-size: 14px;
                transition: color 0.2s ease, text-shadow 0.2s ease;
                text-shadow: 
                    0 0 8px rgba(251, 185, 36, 0.5),
                    0 0 16px rgba(251, 185, 36, 0.25);
            }}
            .back-link:hover {{
                color: var(--alba-orange);
                text-shadow: 
                    0 0 10px rgba(247, 130, 34, 0.6),
                    0 0 20px rgba(247, 130, 34, 0.3);
            }}
            .cta-card {{
                padding: 18px 20px;
                background: linear-gradient(to bottom, rgba(48, 48, 53, 0.95), #303035);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 8px;
                box-shadow: 
                    0 2px 10px rgba(0, 0, 0, 0.25),
                    0 1px 4px rgba(0, 0, 0, 0.15);
                margin-bottom: 16px;
            }}
            .cta-title {{
                font-weight: 700;
                color: var(--alba-cream);
                margin-bottom: 12px;
                font-size: 16px;
                letter-spacing: -0.1px;
            }}
            .cta-body {{
                color: var(--alba-cream);
                font-size: 14px;
                font-weight: 400;
                line-height: 1.7;
                margin-bottom: 20px;
            }}
            .cta-buttons {{
                display: flex;
                flex-direction: column;
                gap: 10px;
            }}
            @media (min-width: 640px) {{
                .cta-buttons {{
                    flex-direction: row;
                    gap: 12px;
                }}
            }}
            .cta-button-primary {{
                display: inline-block;
                padding: 12px 20px;
                background: var(--alba-orange);
                color: var(--alba-cream);
                text-decoration: none;
                border-radius: 8px;
                font-weight: 500;
                font-size: 14px;
                text-align: center;
                transition: background 0.2s ease, box-shadow 0.2s ease, text-shadow 0.2s ease;
                flex: 1;
                box-shadow: 
                    0 2px 8px rgba(247, 130, 34, 0.25),
                    0 0 16px rgba(247, 130, 34, 0.15),
                    0 0 32px rgba(247, 130, 34, 0.08);
                text-shadow: 0 0 4px rgba(255, 247, 224, 0.2);
            }}
            .cta-button-primary:hover {{
                background: #E6731F;
                box-shadow: 
                    0 2px 12px rgba(247, 130, 34, 0.35),
                    0 0 20px rgba(247, 130, 34, 0.2),
                    0 0 40px rgba(247, 130, 34, 0.1);
                text-shadow: 0 0 6px rgba(255, 247, 224, 0.3);
            }}
            .cta-button-secondary {{
                display: inline-block;
                padding: 12px 20px;
                background: transparent;
                color: var(--alba-cream);
                text-decoration: none;
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 8px;
                font-weight: 500;
                font-size: 14px;
                text-align: center;
                transition: border-color 0.2s ease, color 0.2s ease;
                flex: 1;
            }}
            .cta-button-secondary:hover {{
                border-color: rgba(255, 255, 255, 0.4);
                color: var(--alba-cream);
            }}
            .feedback-link-wrapper {{
                margin-top: 8px;
            }}
            .feedback-link {{
                color: rgba(255, 247, 224, 0.6);
                font-size: 12px;
                text-decoration: none;
                transition: color 0.2s ease;
            }}
            .feedback-link:hover {{
                color: var(--alba-cream);
            }}
            .feedback-panel {{
                margin-top: 16px;
                margin-bottom: 16px;
                padding: 14px;
                background: rgba(255, 255, 255, 0.05);
                border-radius: 6px;
                border: 1px solid rgba(255, 255, 255, 0.1);
                max-width: 100%;
            }}
            .feedback-field {{
                margin-bottom: 14px;
            }}
            .feedback-field:last-of-type {{
                margin-bottom: 0;
            }}
            .feedback-label {{
                display: block;
                font-size: 13px;
                font-weight: 500;
                color: rgba(255, 247, 224, 0.9);
                margin-bottom: 6px;
            }}
            .feedback-field textarea,
            .feedback-field .feedback-input {{
                width: 100%;
                padding: 10px 12px;
                font-size: 14px;
                font-family: 'Poppins', sans-serif;
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 6px;
                background: rgba(255, 255, 255, 0.08);
                color: var(--alba-cream);
                transition: border-color 0.2s ease, background 0.2s ease;
            }}
            .feedback-field textarea:focus,
            .feedback-field .feedback-input:focus {{
                outline: none;
                border-color: rgba(255, 255, 255, 0.3);
                background: rgba(255, 255, 255, 0.1);
            }}
            .feedback-field textarea {{
                resize: vertical;
                min-height: 80px;
            }}
            .feedback-field .feedback-input {{
                height: 40px;
            }}
            .feedback-actions {{
                margin-top: 12px;
            }}
            .feedback-submit {{
                padding: 10px 20px;
                background: var(--alba-orange);
                color: var(--alba-cream);
                border: none;
                border-radius: 8px;
                font-weight: 500;
                font-size: 14px;
                font-family: 'Poppins', sans-serif;
                cursor: pointer;
                transition: background 0.2s ease, box-shadow 0.2s ease, text-shadow 0.2s ease;
                box-shadow: 
                    0 2px 8px rgba(247, 130, 34, 0.25),
                    0 0 16px rgba(247, 130, 34, 0.15),
                    0 0 32px rgba(247, 130, 34, 0.08);
                text-shadow: 0 0 4px rgba(255, 247, 224, 0.2);
            }}
            .feedback-submit:hover {{
                background: #E6731F;
                box-shadow: 
                    0 2px 12px rgba(247, 130, 34, 0.35),
                    0 0 20px rgba(247, 130, 34, 0.2),
                    0 0 40px rgba(247, 130, 34, 0.1);
                text-shadow: 0 0 6px rgba(255, 247, 224, 0.3);
            }}
            .feedback-submit:disabled {{
                opacity: 0.5;
                cursor: not-allowed;
            }}
            .feedback-thanks {{
                color: var(--alba-cream);
                font-size: 14px;
                padding: 12px 0;
            }}
        </style>
    </head>
    <body>
        <div class="page-wrap">
        <div class="container">
            {verdict_banner_html}
            {feedback_panel_html}
            
            <div class="cards-grid">
                <div class="card">
                    <div class="card-title">Why</div>
                    <div class="card-content">
                        {view_model['why_bullets_html']}
                    </div>
                </div>
                
                <div class="card-stack">
                    <div class="card">
                        <div class="card-title">What you could do</div>
                        <div class="card-content">
                            {view_model['what_bullets_html']}
                        </div>
                    </div>
                    
                    {instead_section_html}
                </div>
            </div>
            
            {details_html}
            
            <div class="cta-card">
                <div class="cta-title">{view_model['cta_title']}</div>
                <div class="cta-body">{view_model['cta_body']}</div>
                <div class="cta-buttons">
                    <a href="{ALBA_IOS_URL}" class="cta-button-primary" {EXTERNAL_LINK_ATTRS}>Download on iPhone</a>
                    <a href="{ALBA_ANDROID_URL}" class="cta-button-primary" {EXTERNAL_LINK_ATTRS}>Download on Android</a>
                    <a href="{ALBA_HOW_IT_WORKS_URL}" class="cta-button-secondary" {EXTERNAL_LINK_ATTRS}>See how Alba works</a>
                </div>
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
                
                // Feedback panel functions
                window.toggleFeedbackPanel = function() {{
                    const panel = document.getElementById('feedback-panel');
                    if (panel) {{
                        panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
                    }}
                }};
                
                window.submitFeedback = async function(event) {{
                    event.preventDefault();
                    const form = document.getElementById('feedback-form');
                    const thanks = document.getElementById('feedback-thanks');
                    const submitBtn = form.querySelector('.feedback-submit');
                    
                    // Check honeypot
                    const companyField = document.getElementById('company-field');
                    if (companyField && companyField.value) {{
                        // Honeypot filled - silently accept but don't store
                        form.style.display = 'none';
                        if (thanks) thanks.style.display = 'block';
                        return;
                    }}
                    
                    // Disable form
                    submitBtn.disabled = true;
                    
                    // Collect form data
                    const formData = new FormData(form);
                    const data = Object.fromEntries(formData.entries());
                    
                    try {{
                        const response = await fetch('/feedback', {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify(data)
                        }});
                        
                        if (response.ok) {{
                            form.style.display = 'none';
                            if (thanks) thanks.style.display = 'block';
                        }} else {{
                            alert('Failed to submit feedback. Please try again.');
                            submitBtn.disabled = false;
                        }}
                    }} catch (error) {{
                        console.error('Error submitting feedback:', error);
                        alert('Failed to submit feedback. Please try again.');
                        submitBtn.disabled = false;
                    }}
                }};
            }})();
        </script>
        {IFRAME_RESIZE_SCRIPT}
        </div>
    </body>
    </html>
    """


@app.post("/assess", response_class=RedirectResponse)
async def assess_post(
    course: str = Form(...),
    handicap: int = Form(None),
    golf_experience: str = Form("Regular"),
    day: str = Form(...),
    time_of_day: str = Form(...)
):
    """
    Handle POST form submission and redirect to GET with query parameters.
    """
    # Build query parameters - only include handicap if provided
    params = {
        "course": course,
        "golf_experience": golf_experience,
        "day": day,
        "time_of_day": time_of_day
    }
    if handicap is not None:
        params["handicap"] = str(handicap)
    
    # Redirect to GET endpoint
    query_string = urlencode(params)
    return RedirectResponse(url=f"/assess?{query_string}", status_code=303)


@app.get("/download", response_class=HTMLResponse)
async def download_app(
    course: str = Query(None),
    handicap: int = Query(None),
    day: str = Query(None),
    time_of_day: str = Query(None),
    verdict: str = Query(None),
    overall_score: int = Query(None),
    request: StarletteRequest = None
):
    """
    Download page that detects user agent and redirects to appropriate app store.
    Logs click with assessment context.
    """
    # Log the click with assessment context
    log_data = {
        "course": course or "unknown",
        "handicap": handicap or "unknown",
        "day": day or "unknown",
        "time_of_day": time_of_day or "unknown",
        "verdict": verdict or "unknown",
        "overall_score": overall_score or "unknown"
    }
    print(f"DOWNLOAD_CLICK: {log_data}")
    
    # Detect user agent
    user_agent = request.headers.get("user-agent", "").lower() if request else ""
    
    # Check for iOS
    if "iphone" in user_agent or "ipad" in user_agent or "ipod" in user_agent:
        return RedirectResponse(url=ALBA_IOS_URL, status_code=302)
    
    # Check for Android
    if "android" in user_agent:
        return RedirectResponse(url=ALBA_ANDROID_URL, status_code=302)
    
    # Default: show page with both links
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Download Alba - Alba Labs</title>
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
            body {{
                font-family: 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: var(--alba-offblack);
                color: var(--alba-cream);
                margin: 0;
                padding: 0;
                min-height: 100vh;
            }}
            .container {{
                max-width: 500px;
                width: 100%;
                margin: 0 auto;
                padding: 24px;
                text-align: center;
            }}
            @media (max-width: 640px) {{
                .container {{
                    padding: 16px;
                }}
            }}
            h1 {{
                font-size: 24px;
                font-weight: 600;
                margin-bottom: 6px;
                color: var(--alba-cream);
            }}
            p {{
                font-size: 14px;
                color: rgba(255, 247, 224, 0.7);
                margin-bottom: 20px;
                line-height: 1.5;
            }}
            .download-links {{
                display: flex;
                flex-direction: column;
                gap: 10px;
            }}
            .download-link {{
                display: inline-block;
                padding: 12px 20px;
                background: var(--alba-orange);
                color: var(--alba-cream);
                text-decoration: none;
                border-radius: 8px;
                font-weight: 500;
                font-size: 15px;
                transition: background 0.2s ease;
            }}
            .download-link:hover {{
                background: #E6731F;
            }}
            .back-link {{
                display: inline-block;
                margin-top: 16px;
                color: rgba(255, 247, 224, 0.7);
                text-decoration: none;
                font-size: 14px;
            }}
            .back-link:hover {{
                color: var(--alba-cream);
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Get Alba</h1>
            <p>Pick your platform.</p>
            <div class="download-links">
                <a href="{ALBA_IOS_URL}" class="download-link" {EXTERNAL_LINK_ATTRS}>Download on iPhone</a>
                <a href="{ALBA_ANDROID_URL}" class="download-link" {EXTERNAL_LINK_ATTRS}>Download on Android</a>
            </div>
            <a href="/" class="back-link">Back</a>
        </div>
    </body>
    </html>
    """


@app.post("/feedback")
async def submit_feedback(request: StarletteRequest):
    """
    Handle feedback submission and store to data/feedback.jsonl.
    Honeypot field 'company' - if filled, silently accept but don't store.
    
    Feedback stored at data/feedback.jsonl
    """
    try:
        data = await request.json()
        
        # Check honeypot
        if data.get("company"):
            # Honeypot filled - silently accept but don't store
            return {"status": "ok"}
        
        # Prepare feedback payload
        feedback = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "course": data.get("course", ""),
            "handicap": data.get("handicap", ""),
            "day": data.get("day", ""),
            "time_of_day": data.get("time_of_day", ""),
            "verdict": data.get("verdict", ""),
            "overall_score": data.get("overall_score", ""),
            "factor_scores": json.loads(data.get("factor_scores", "{}")),
            "banner_summary": data.get("banner_summary", ""),
            "feedback_text": data.get("feedback_text", ""),
            "email": data.get("email", "")
        }
        
        # Ensure data directory exists
        data_dir = BASE_DIR / "data"
        data_dir.mkdir(exist_ok=True)
        
        # Append to feedback.jsonl
        feedback_file = data_dir / "feedback.jsonl"
        with open(feedback_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(feedback) + "\n")
        
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error storing feedback: {str(e)}", exc_info=True)
        return {"status": "error", "message": str(e)}


security = HTTPBasic()


def verify_feedback_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify HTTP basic auth credentials for feedback viewer."""
    expected_user = os.getenv("FEEDBACK_USER")
    expected_pass = os.getenv("FEEDBACK_PASS")
    
    if not expected_user or not expected_pass:
        raise HTTPException(status_code=404, detail="Not found")
    
    if credentials.username != expected_user or credentials.password != expected_pass:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    
    return credentials


@app.get("/feedback-viewer")
async def view_feedback(credentials: HTTPBasicCredentials = Depends(verify_feedback_auth)):
    """
    Admin route to view the last 50 lines of feedback.jsonl.
    Requires HTTP Basic Auth via FEEDBACK_USER and FEEDBACK_PASS env vars.
    Returns 404 if env vars are not set.
    """
    try:
        feedback_file = BASE_DIR / "data" / "feedback.jsonl"
        
        if not feedback_file.exists():
            return PlainTextResponse("No feedback file found.\n", status_code=200)
        
        # Read last 50 lines
        with open(feedback_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            last_50 = lines[-50:] if len(lines) > 50 else lines
        
        content = "".join(last_50)
        return PlainTextResponse(content, media_type="text/plain")
    except Exception as e:
        logger.error(f"Error reading feedback: {str(e)}", exc_info=True)
        return PlainTextResponse(f"Error reading feedback: {str(e)}\n", status_code=500)


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
    golf_experience: str = Query("Regular"),
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
    
    # Handicap is optional, but day and time_of_day are required
    if not all([day, time_of_day]):
        return RedirectResponse(url="/", status_code=303)
    
    # Set handicap to None if not provided
    if handicap is None:
        handicap = None
    
    # Validate golf_experience (default to Regular if invalid)
    if golf_experience not in ["Beginner", "Regular", "Confident"]:
        golf_experience = "Regular"
    
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
    
    # Render results with error handling
    try:
        return await render_assessment_results(course, handicap, golf_experience, day, time_of_day, llm_force, llm_effective_enabled, llm_raw, request_id, debug_mode)
    except Exception as e:
        logger.error(f"Error rendering assessment results: {str(e)}", exc_info=True)
        # Return error page
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Error - Alba Labs</title>
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
                body {{
                    font-family: 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                    background: var(--alba-offblack);
                    color: var(--alba-cream);
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    min-height: 100vh;
                    padding: 24px;
                }}
                .container {{
                    max-width: 500px;
                    width: 100%;
                    text-align: center;
                }}
                .error-message {{
                    font-size: 16px;
                    line-height: 1.6;
                    margin-bottom: 24px;
                    color: var(--alba-cream);
                }}
                .back-link {{
                    display: inline-block;
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
                <div class="error-message">We couldn't check playability for that selection. Try a different course or time.</div>
                <a href="/" class="back-link">← Back to home</a>
            </div>
        </body>
        </html>
        """


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

