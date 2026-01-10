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


def calculate_weather_rating(weather_data):
    """
    Calculate weather rating: Good / Mixed / Poor
    Based on temperature, wind speed, and precipitation.
    """
    if not weather_data:
        return "Mixed"
    
    temp_avg = (weather_data["temperature_min"] + weather_data["temperature_max"]) / 2
    wind_speed = weather_data["wind_speed"]
    precipitation = weather_data["precipitation"]
    
    score = 0
    
    # Temperature scoring (ideal: 15-25°C)
    if 15 <= temp_avg <= 25:
        score += 2
    elif 10 <= temp_avg < 15 or 25 < temp_avg <= 30:
        score += 1
    
    # Wind scoring (ideal: < 20 km/h)
    if wind_speed < 20:
        score += 2
    elif wind_speed < 30:
        score += 1
    
    # Precipitation scoring (ideal: < 1mm)
    if precipitation < 1:
        score += 2
    elif precipitation < 5:
        score += 1
    
    if score >= 5:
        return "Good"
    elif score >= 3:
        return "Mixed"
    else:
        return "Poor"


def calculate_ground_condition(historical_rainfall):
    """
    Calculate ground condition: Firm / Mixed / Soft / Soggy
    Based on last 7 days rainfall.
    """
    if historical_rainfall is None:
        return "Mixed"
    
    if historical_rainfall < 5:
        return "Firm"
    elif historical_rainfall < 15:
        return "Mixed"
    elif historical_rainfall < 30:
        return "Soft"
    else:
        return "Soggy"


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


def calculate_busyness_rating(month, weather_rating, day_of_week, time_of_day, popularity_tier):
    """
    Calculate busyness rating: Quiet / Moderate / Busy / Very busy
    Factors: seasonality (month), weather attractiveness, day of week, time of day, popularity tier
    """
    score = 0
    
    # Seasonality (UK golf season: April-September peak)
    if month in [4, 5, 6, 7, 8, 9]:
        score += 2
    elif month in [3, 10]:
        score += 1
    
    # Weather attractiveness
    if weather_rating == "Good":
        score += 2
    elif weather_rating == "Mixed":
        score += 1
    
    # Day of week (weekend = busier)
    if day_of_week in [5, 6]:  # Saturday, Sunday
        score += 2
    elif day_of_week == 4:  # Friday
        score += 1
    
    # Time of day (midday and afternoon busier)
    if time_of_day == "Midday":
        score += 2
    elif time_of_day == "Afternoon":
        score += 2
    elif time_of_day == "Morning":
        score += 1
    
    # Popularity tier
    if popularity_tier == "High":
        score += 2
    elif popularity_tier == "Medium":
        score += 1
    
    if score >= 8:
        return "Very busy"
    elif score >= 5:
        return "Busy"
    elif score >= 3:
        return "Moderate"
    else:
        return "Quiet"


def calculate_handicap_suitability(handicap, course_difficulty, busyness_rating):
    """
    Calculate handicap suitability: Well suited / Borderline / Not ideal today
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
    
    if total_score >= 2:
        return "Well suited"
    elif total_score >= 0:
        return "Borderline"
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
    
    if weather_rating == "Good":
        score += 2
    elif weather_rating == "Mixed":
        score += 1
    
    if ground_condition in ["Firm", "Mixed"]:
        score += 1
    
    if busyness_rating in ["Quiet", "Moderate"]:
        score += 2
    elif busyness_rating == "Busy":
        score += 1
    
    if handicap_suitability == "Well suited":
        score += 2
    elif handicap_suitability == "Borderline":
        score += 1
    
    return "Play" if score >= 5 else "Don't play"


def generate_added_action(play_recommendation, time_of_day, busyness_rating, weather_rating, handicap_suitability):
    """
    Generate added action suggestions.
    If Play: suggest best time window and social vs PB attempt
    If Don't play: suggest tomorrow or quieter time and practical alternative
    """
    if play_recommendation == "Play":
        # Suggest best time window
        if busyness_rating in ["Quiet", "Moderate"]:
            time_suggestion = "Any time window should work well"
        elif time_of_day == "Morning":
            time_suggestion = "Morning is ideal for avoiding crowds"
        elif time_of_day == "Evening":
            time_suggestion = "Evening offers quieter conditions"
        else:
            time_suggestion = "Consider Morning or Evening for quieter conditions"
        
        # Social vs PB attempt
        if weather_rating == "Good" and handicap_suitability == "Well suited":
            round_type = "This is a good day for a personal best attempt"
        elif weather_rating == "Good":
            round_type = "Good conditions for a social round"
        else:
            round_type = "Better suited for a social round"
        
        return f"{time_suggestion}. {round_type}."
    else:
        # Don't play suggestions
        suggestions = []
        
        if weather_rating == "Poor":
            suggestions.append("Consider playing tomorrow if weather improves")
        
        if busyness_rating in ["Busy", "Very busy"]:
            suggestions.append("Try a quieter time of day like Early Morning or Evening")
        
        if handicap_suitability == "Not ideal today":
            suggestions.append("Consider a course with easier difficulty or wait for quieter conditions")
        
        if not suggestions:
            suggestions.append("Consider playing tomorrow or choosing a different course")
        
        return " ".join(suggestions)


def generate_why_bullets(play_recommendation, weather_rating, ground_condition, busyness_rating, handicap_suitability, daylight_label):
    """
    Generate exactly 3 bullet points explaining why the verdict was given.
    Returns a list of 3 strings.
    """
    bullets = []
    
    # Priority order: daylight, weather, ground, busyness, handicap
    # Always include the most significant factors
    
    if daylight_label == "Not feasible":
        bullets.append("Not enough daylight to complete your round safely")
    elif daylight_label == "Tight":
        bullets.append("Daylight is tight, so you'll need to keep a good pace")
    
    if weather_rating == "Poor":
        bullets.append("Weather conditions are challenging today")
    elif weather_rating == "Good":
        bullets.append("Weather looks good for golf")
    
    if ground_condition == "Soggy":
        bullets.append("The ground is very wet from recent rain")
    elif ground_condition == "Firm":
        bullets.append("Ground conditions are firm and playable")
    
    if busyness_rating in ["Very busy", "Busy"]:
        bullets.append("The course is likely to be busy")
    elif busyness_rating == "Quiet":
        bullets.append("Expect quieter conditions on the course")
    
    if handicap_suitability == "Not ideal today":
        bullets.append("Course difficulty and conditions may not suit your handicap today")
    elif handicap_suitability == "Well suited":
        bullets.append("Course conditions suit your handicap well")
    
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
    
    # Ensure exactly 3 bullets
    while len(priority_bullets) < 3:
        if play_recommendation == "Play":
            priority_bullets.append("Overall conditions are favourable for a round")
        else:
            priority_bullets.append("Multiple factors suggest waiting for better conditions")
    
    return priority_bullets[:3]


def generate_what_to_do(play_recommendation, weather_rating, busyness_rating, handicap_suitability, daylight_label, recommended_holes, time_of_day, day):
    """
    Generate practical advice section.
    If Play: "What to expect"
    If Don't play: "What to do instead"
    """
    if play_recommendation == "Play":
        advice_parts = []
        
        if recommended_holes == 9:
            advice_parts.append(f"Plan for {recommended_holes} holes to ensure you finish in daylight")
        else:
            advice_parts.append(f"{recommended_holes} holes should be manageable")
        
        if busyness_rating in ["Quiet", "Moderate"]:
            advice_parts.append("You should have plenty of space on the course")
        elif busyness_rating in ["Busy", "Very busy"]:
            advice_parts.append("Be prepared for slower play due to busy conditions")
        
        if weather_rating == "Good":
            advice_parts.append("Enjoy the good weather conditions")
        elif weather_rating == "Mixed":
            advice_parts.append("Keep an eye on the weather as conditions may change")
        
        return " ".join(advice_parts) if advice_parts else "Enjoy your round"
    else:
        # Don't play - what to do instead
        alternatives = []
        
        if daylight_label == "Not feasible":
            alternatives.append("Try booking an earlier tee time tomorrow")
        elif daylight_label == "Tight":
            alternatives.append(f"Consider playing {recommended_holes} holes instead, or start earlier")
        
        if weather_rating == "Poor":
            if day == "Today":
                alternatives.append("Check tomorrow's forecast for better conditions")
            else:
                alternatives.append("Wait for a day with better weather")
        
        if busyness_rating in ["Busy", "Very busy"]:
            alternatives.append("Try booking at a quieter time, like early morning or late afternoon")
        
        if handicap_suitability == "Not ideal today":
            alternatives.append("Consider trying a different course that better matches your skill level")
        
        if not alternatives:
            alternatives.append("Try again tomorrow or choose a different time slot")
        
        return " ".join(alternatives[:2]) if len(alternatives) >= 2 else alternatives[0] if alternatives else "Consider trying again another day"


def get_price_label(price_tier: str) -> str:
    """
    Convert price tier symbol to descriptive label.
    Returns: "Affordable", "Mid-range", "Expensive", or "Unknown"
    """
    if price_tier == "£":
        return "Affordable"
    elif price_tier == "££":
        return "Mid-range"
    elif price_tier == "£££":
        return "Expensive"
    else:
        return "Unknown"


def generate_explanation_deterministic(weather_rating, ground_condition, busyness_rating, handicap_suitability, price_label, tomorrow_forecast, recommended_holes=None):
    """
    Generate deterministic explanation paragraph summarising all ratings.
    """
    parts = []
    
    parts.append(f"The weather conditions are rated as {weather_rating.lower()}")
    if tomorrow_forecast:
        parts.append(f"with tomorrow's forecast {tomorrow_forecast.lower()}")
    parts.append(f"and the ground condition is {ground_condition.lower()} based on recent rainfall.")
    
    parts.append(f"The course busyness is estimated as {busyness_rating.lower()} (not live tee times)")
    parts.append(f"considering seasonality, weather attractiveness, day of week, time of day, and course popularity.")
    
    parts.append(f"For your handicap, this course is {handicap_suitability.lower()} today.")
    
    if recommended_holes:
        if recommended_holes == 18:
            parts.append(f"{recommended_holes} holes looks feasible before sunset.")
        else:
            parts.append(f"{recommended_holes} holes is the safer call for daylight.")
    
    parts.append(f"The price tier is {price_label.lower()} (typical estimate).")
    
    return " ".join(parts)


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
        - suitability_rating: str (Well suited/Borderline/Not ideal today)
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
        if recommended_holes == 9:
            next_step = "9 holes at midday is the safer call, or try morning tomorrow"
        else:
            if user_day == "Today":
                next_step = "Try morning tomorrow or a quieter course"
            else:
                next_step = "Try morning or a quieter course"
    else:
        # Use next_action if available, otherwise provide a generic positive action
        next_step = assessment_data.get("next_action", "Enjoy your round")
    
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
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            html, body {{
                height: auto;
            }}
            body {{
                font-family: 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: #2C2C2F;
                padding: 24px 16px;
                line-height: 1.6;
                color: #FFF7E0;
            }}
            .container {{
                max-width: 720px;
                margin: 0 auto;
            }}
            .form-card {{
                background: #303035;
                border-radius: 12px;
                padding: 24px 20px;
                border: 1px solid rgba(255, 255, 255, 0.05);
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
            }}
            .form-header {{
                margin-bottom: 20px;
                text-align: center;
            }}
            .form-title {{
                color: #FFF7E0;
                font-weight: 500;
                font-size: 20px;
                margin-bottom: 6px;
                letter-spacing: -0.2px;
            }}
            .form-subtitle {{
                color: rgba(255, 247, 224, 0.7);
                font-weight: 300;
                font-size: 13px;
                line-height: 1.4;
            }}
            @media (max-width: 640px) {{
                .form-title {{
                    font-size: 18px;
                }}
                .form-subtitle {{
                    font-size: 12px;
                }}
            }}
            form {{
                margin-top: 0;
            }}
            .form-group {{
                margin-bottom: 16px;
            }}
            .form-group:last-of-type {{
                margin-bottom: 20px;
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
                color: #FFF7E0;
                letter-spacing: 0.1px;
            }}
            select, input[type="number"], input[type="text"] {{
                width: 100%;
                padding: 10px 14px;
                font-size: 15px;
                font-family: 'Poppins', sans-serif;
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 8px;
                background: #2C2C2F;
                transition: all 0.2s ease;
                color: #FFF7E0;
            }}
            select:focus, input[type="number"]:focus, input[type="text"]:focus {{
                outline: none;
                border-color: #F78222;
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
                gap: 6px;
                margin-top: 8px;
            }}
            .course-chip {{
                display: inline-block;
                padding: 4px 10px;
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 12px;
                font-size: 11px;
                color: rgba(255, 247, 224, 0.7);
                cursor: pointer;
                transition: all 0.2s ease;
                font-weight: 400;
            }}
            .course-chip:hover {{
                background: rgba(247, 130, 34, 0.2);
                border-color: #F78222;
                color: #FFF7E0;
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
                color: #FFF7E0;
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
                color: #E23642;
                margin-top: 6px;
                font-weight: 400;
                display: none;
            }}
            .error-message.show {{
                display: block;
            }}
            .primary-button {{
                background: #F78222;
                color: #000000;
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
            @media (min-width: 641px) {{
                .primary-button {{
                    width: auto;
                    min-width: 180px;
                }}
            }}
            .primary-button:hover {{
                background: #FBB924;
                transform: translateY(-1px);
            }}
            .primary-button:active {{
                transform: translateY(0);
            }}
            .button-wrapper {{
                display: flex;
                justify-content: center;
                margin-top: 4px;
            }}
            .build-footer {{
                font-size: 11px;
                color: rgba(255, 247, 224, 0.4);
                text-align: center;
                margin-top: 24px;
                padding-top: 16px;
                font-weight: 300;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="form-card">
                <div class="form-header">
                    <h1 class="form-title">Playability check</h1>
                    <p class="form-subtitle">A quick read on weather, ground, and how the round might feel.</p>
                </div>
                <form method="post" action="/assess">
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
                    </div>
                    
                    <div class="form-row">
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
                    
                    <div class="button-wrapper">
                        <button type="submit" class="primary-button">Check playability</button>
                    </div>
                </form>
            </div>
        <div class="build-footer">Build: {BUILD_TIME_UTC}</div>
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
    weather_rating = calculate_weather_rating(weather_data)
    ground_condition = calculate_ground_condition(historical_rainfall)
    tomorrow_forecast = None
    if day == "Today" and tomorrow_weather:
        tomorrow_forecast = calculate_tomorrow_forecast(weather_data, tomorrow_weather)
    
    busyness_rating = calculate_busyness_rating(
        month, weather_rating, day_of_week, time_of_day, 
        course_data["popularity_tier"] if course_data else "Medium"
    )
    
    handicap_suitability = calculate_handicap_suitability(
        handicap,
        course_data["difficulty"] if course_data else "Medium",
        busyness_rating
    )
    
    # Calculate daylight feasibility
    daylight_info = calculate_daylight_feasibility(
        time_of_day, busyness_rating, weather_data, month, target_date
    )
    recommended_holes = daylight_info["recommended_holes"]
    daylight_label = daylight_info["daylight_label"]
    finish_time_estimate = daylight_info["finish_time_estimate"]
    sunset_time = daylight_info["sunset_time"]
    
    play_recommendation = determine_play_recommendation(
        weather_rating, ground_condition, busyness_rating, handicap_suitability, daylight_label
    )
    
    added_action = generate_added_action(
        play_recommendation, time_of_day, busyness_rating, weather_rating, handicap_suitability
    )
    
    # Generate why bullets and what to do sections
    why_bullets = generate_why_bullets(
        play_recommendation, weather_rating, ground_condition, busyness_rating, 
        handicap_suitability, daylight_label
    )
    what_to_do = generate_what_to_do(
        play_recommendation, weather_rating, busyness_rating, handicap_suitability,
        daylight_label, recommended_holes, time_of_day, day
    )
    
    # Convert price_tier to price_label
    price_tier_raw = course_data["price_tier"] if course_data else "££"
    price_label = get_price_label(price_tier_raw)
    
    # Set final_summary to deterministic by default
    final_summary = generate_explanation_deterministic(
        weather_rating,
        ground_condition,
        busyness_rating,
        handicap_suitability,
        price_label,
        tomorrow_forecast,
        recommended_holes
    )
    
    # Determine summary_mode based on whether LLM was attempted
    summary_mode = "Deterministic"
    
    # If llm_effective_enabled is true, try to call OpenAI summary function
    if llm_effective_enabled:
        # Create structured assessment data for LLM
        assessment_data = {
            "course_name": course,
            "day": day,
            "time_of_day": time_of_day,
            "handicap": handicap,
            "weather_rating": weather_rating,
            "ground_rating": ground_condition,
            "busyness_rating": busyness_rating,
            "suitability_rating": handicap_suitability,
            "price_label": price_label,
            "verdict": play_recommendation,
            "next_action": added_action,
            "recommended_holes": recommended_holes,
            "daylight_label": daylight_label
        }
        
        try:
            llm_summary = await generate_explanation_llm(assessment_data, request_id)
            final_summary = llm_summary
            summary_mode = "LLM"
        except Exception as e:
            # Log the exception and keep final_summary unchanged (stays as deterministic)
            logger.error(f"OpenAI summary failed: {str(e)}", exc_info=True)
            summary_mode = "Deterministic (LLM failed)"
    
    # Build HTML sections in exact order specified
    # 1. Weather rating
    weather_rating_html = f"""
        <div class="result-item">
            <div class="result-label">Weather Rating</div>
            <div class="result-value">{weather_rating}</div>
        </div>
        <div class="result-item">
            <div class="result-label">Ground</div>
            <div class="result-value">{ground_condition}</div>
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
    
    # 3. Handicap suitability
    handicap_html = f"""
        <div class="result-item">
            <div class="result-label">Handicap Suitability</div>
            <div class="result-value">{handicap_suitability}</div>
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
    
    # Convert play_recommendation to user-friendly verdict
    if play_recommendation == "Play":
        verdict_text = "Worth playing"
        verdict_color = "#FFF7E0"
    else:
        verdict_text = "Not ideal today"
        verdict_color = "#FFF7E0"
    
    # 5. Explanation paragraph - break into sentences for better scanability
    # Determine mode badge text based on llm_effective_enabled
    if llm_effective_enabled:
        if summary_mode == "LLM":
            mode_badge_text = "Mode: LLM"
        else:
            mode_badge_text = "Mode: Deterministic (LLM failed)"
    else:
        mode_badge_text = "Mode: Deterministic"
    
    # Split summary into sentences for better readability
    summary_sentences = [s.strip() for s in final_summary.split('.') if s.strip()]
    summary_html_content = '<br><br>'.join([f'<p style="margin: 0;">{sentence}.</p>' for sentence in summary_sentences])
    
    explanation_html = f"""
        <div class="result-item">
            <div class="result-label">Summary<span class="mode-badge">{mode_badge_text}</span></div>
            <div class="result-value">{summary_html_content}</div>
        </div>
    """
    
    # 6. Why section with 3 bullets - dedupe if needed
    unique_bullets = []
    seen_bullets = set()
    for bullet in why_bullets:
        bullet_lower = bullet.lower()
        if bullet_lower not in seen_bullets:
            unique_bullets.append(bullet)
            seen_bullets.add(bullet_lower)
    # Ensure we have exactly 3 bullets
    while len(unique_bullets) < 3:
        if play_recommendation == "Play":
            fallback = "Overall conditions are favourable for a round"
        else:
            fallback = "Multiple factors suggest waiting for better conditions"
        if fallback.lower() not in seen_bullets:
            unique_bullets.append(fallback)
            seen_bullets.add(fallback.lower())
        else:
            break
    
    why_bullets_html = '<ul class="why-bullets">' + ''.join([f'<li>{bullet}</li>' for bullet in unique_bullets[:3]]) + '</ul>'
    why_html = f"""
        <div class="result-item">
            <div class="result-label">Why</div>
            <div class="result-value">{why_bullets_html}</div>
        </div>
    """
    
    # 7. What to do instead / What to expect
    what_section_title = "What to expect" if play_recommendation == "Play" else "What to do instead"
    what_html = f"""
        <div class="result-item">
            <div class="result-label">{what_section_title}</div>
            <div class="result-value">{what_to_do}</div>
        </div>
    """
    
    # Verdict banner
    verdict_banner_bg = "#4A9B5A" if play_recommendation == "Play" else "#E23642"  # Modern green for Play, red for Don't play
    verdict_banner_html = f"""
        <div class="verdict-banner" style="background: {verdict_banner_bg};">
            <div class="verdict-banner-text">{verdict_text}</div>
        </div>
    """
    
    # Compact header with course name and day/time
    compact_header_html = f"""
        <div class="compact-header">
            <div class="course-name-compact">{course}</div>
            <div class="day-time-compact">{day} · {time_of_day}</div>
        </div>
    """
    
    # Debug info (only shown if debug_mode is True)
    debug_info_html = ""
    if debug_mode:
        debug_info_html = f"""
        <div class="result-item debug-info">
            <div class="result-label">Debug</div>
            <div class="result-value">
                llm_raw: {llm_raw if llm_raw is not None else 'None'}<br>
                llm_force: {force_llm}<br>
                effective: {llm_effective_enabled}
            </div>
        </div>
        """
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Assessment Results - Alba Labs</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600&display=swap" rel="stylesheet">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            html, body {{
                height: auto;
            }}
            body {{
                font-family: 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: #2C2C2F;
                padding: 24px 16px;
                line-height: 1.6;
                color: #FFF7E0;
            }}
            .container {{
                max-width: 720px;
                margin: 0 auto;
            }}
            .verdict-banner {{
                border-radius: 8px;
                padding: 14px 20px;
                margin-bottom: 16px;
                text-align: center;
            }}
            .verdict-banner-text {{
                color: #FFF7E0;
                font-weight: 500;
                font-size: 18px;
                letter-spacing: -0.2px;
            }}
            @media (max-width: 640px) {{
                .verdict-banner-text {{
                    font-size: 16px;
                }}
            }}
            .compact-header {{
                margin-bottom: 20px;
                text-align: center;
            }}
            .course-name-compact {{
                font-size: 16px;
                color: #FFF7E0;
                font-weight: 500;
                margin-bottom: 4px;
            }}
            .day-time-compact {{
                font-size: 13px;
                color: rgba(255, 247, 224, 0.6);
                font-weight: 400;
            }}
            .supporting-content {{
                margin-top: 16px;
            }}
            .why-what-grid {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 12px;
                margin-bottom: 12px;
            }}
            @media (max-width: 640px) {{
                .why-what-grid {{
                    grid-template-columns: 1fr;
                }}
            }}
            .debug-info {{
                display: none;
            }}
            .result-item {{
                margin-bottom: 12px;
                padding: 16px 20px;
                background: #303035;
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 8px;
                box-shadow: 0 1px 4px rgba(0, 0, 0, 0.2);
                transition: transform 0.2s ease, box-shadow 0.2s ease;
            }}
            .result-item:hover {{
                transform: translateY(-1px);
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
            }}
            .result-label {{
                font-weight: 500;
                color: rgba(255, 247, 224, 0.8);
                margin-bottom: 6px;
                font-size: 12px;
                letter-spacing: 0.3px;
                text-transform: uppercase;
            }}
            .result-value {{
                color: #FFF7E0;
                font-size: 15px;
                font-weight: 400;
                line-height: 1.6;
            }}
            .result-value p {{
                margin-bottom: 10px;
            }}
            .result-value p:last-child {{
                margin-bottom: 0;
            }}
            .why-bullets {{
                list-style: none;
                padding: 0;
                margin: 0;
            }}
            .why-bullets li {{
                padding: 6px 0;
                padding-left: 20px;
                position: relative;
                line-height: 1.6;
            }}
            .why-bullets li:before {{
                content: "•";
                position: absolute;
                left: 6px;
                color: #F78222;
                font-size: 16px;
                line-height: 1.4;
            }}
            .help-text {{
                font-size: 12px;
                color: rgba(255, 247, 224, 0.6);
                margin-top: 6px;
                font-weight: 300;
                font-style: normal;
            }}
            .mode-badge {{
                display: inline-block;
                font-size: 10px;
                padding: 3px 8px;
                background: rgba(247, 130, 34, 0.2);
                border-radius: 6px;
                color: #F78222;
                font-weight: 500;
                margin-left: 8px;
                font-style: normal;
                text-transform: none;
                letter-spacing: 0;
            }}
            .back-link {{
                display: inline-block;
                margin-top: 20px;
                color: #FBB924;
                text-decoration: none;
                font-weight: 500;
                font-size: 14px;
                transition: color 0.2s ease;
            }}
            .back-link:hover {{
                color: #F78222;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            {verdict_banner_html}
            {compact_header_html}
            
            <div class="supporting-content">
                <div class="why-what-grid">
                    {why_html}
                    {what_html}
                </div>
                
                {explanation_html}
                
                {debug_info_html}
                
                <div class="result-item">
                    <div class="result-label">Course</div>
                    <div class="result-value">{course}</div>
                </div>
                
                {weather_rating_html}
                
                {busyness_html}
                
                {handicap_html}
                
                {daylight_html}
                
                {price_html}
            </div>
            
            <a href="/" class="back-link">← Back</a>
        </div>
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
#             Handicap Suitability Borderline/Well suited, Recommendation depends on weather

