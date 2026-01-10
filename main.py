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


def calculate_weather_rating(weather_data):
    """
    Calculate weather rating using condition-based states: Dry, Rainy, Windy, Cold, Poor conditions
    Based on temperature, wind speed, and precipitation.
    Returns the most impactful condition affecting play.
    """
    if not weather_data:
        return "Dry"
    
    temp_avg = (weather_data["temperature_min"] + weather_data["temperature_max"]) / 2
    wind_speed = weather_data["wind_speed"]
    precipitation = weather_data["precipitation"]
    
    # Determine primary condition - prioritize most impactful
    # Rain is most disruptive, then wind, then cold
    if precipitation >= 5:
        return "Rainy"
    elif wind_speed >= 30:
        return "Windy"
    elif temp_avg < 10:
        return "Cold"
    elif precipitation >= 1 or wind_speed >= 20 or temp_avg < 15:
        # Multiple moderate issues = poor conditions
        return "Poor conditions"
    else:
        return "Dry"


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
    if is_weather_favorable(weather_rating):
        score += 2
    elif weather_rating == "Poor conditions":
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
    elif handicap_suitability == "Borderline":
        bullets.append(f"Given your handicap of {handicap}, today's course conditions are borderline for your skill level, so expect some challenging moments")
    
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


def get_ground_label(ground_condition: str) -> str:
    """
    Convert ground condition to explicit, self-explanatory label.
    Returns: "Firm", "Mixed", "Soft (Playable but heavy)", "Too soft (Likely to affect play)", or "Soggy"
    """
    if ground_condition == "Firm":
        return "Firm"
    elif ground_condition == "Mixed":
        return "Mixed"
    elif ground_condition == "Soft":
        return "Soft (Playable but heavy)"
    elif ground_condition == "Soggy":
        return "Too soft (Likely to affect play)"
    else:
        return ground_condition


def get_suitability_label(handicap_suitability: str) -> str:
    """
    Convert handicap suitability to explicit, self-explanatory label.
    Returns: "Well suited", "Borderline", or "Challenging for your handicap today"
    """
    if handicap_suitability == "Well suited":
        return "Well suited"
    elif handicap_suitability == "Borderline":
        return "Borderline"
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
                font-size: 20px;
                margin-bottom: 4px;
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
        handicap_suitability, daylight_label, handicap
    )
    what_to_do = generate_what_to_do(
        play_recommendation, weather_rating, busyness_rating, handicap_suitability,
        daylight_label, recommended_holes, time_of_day, day, handicap, ground_condition
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
    # 1. Weather rating - use explicit labels
    weather_label_display = get_weather_label(weather_rating, weather_data)
    ground_label_display = get_ground_label(ground_condition)
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
    
    # 3. Handicap suitability - use explicit label
    suitability_label_display = get_suitability_label(handicap_suitability)
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
    
    # Convert play_recommendation to user-friendly verdict
    if play_recommendation == "Play":
        verdict_title = "Good to play"
    else:
        verdict_title = "Not ideal today"
    
    # Extract first sentence from summary for banner helper text
    summary_first_sentence = final_summary.split('.')[0].strip() if final_summary else ""
    if summary_first_sentence and not summary_first_sentence.endswith('.'):
        summary_first_sentence += "."
    
    # Why section with 3 bullets - dedupe if needed
    unique_bullets = []
    seen_bullets = set()
    for bullet in why_bullets:
        bullet_lower = bullet.lower()
        if bullet_lower not in seen_bullets:
            unique_bullets.append(bullet)
            seen_bullets.add(bullet_lower)
    # Ensure we have exactly 3 bullets with explanatory content (fallback only)
    # Note: generate_why_bullets should always return 3 bullets, so this is rarely needed
    while len(unique_bullets) < 3:
        # Use available data to create explanatory fallback messages
        if play_recommendation == "Play":
            # Reference the best available condition with explanation
            if is_weather_favorable(weather_rating) and "weather" not in [b.lower() for b in unique_bullets]:
                fallback = "Dry weather conditions today provide ideal ball flight and comfortable playing conditions"
            elif ground_condition in ["Firm", "Mixed"] and "ground" not in [b.lower() for b in unique_bullets]:
                if ground_condition == "Firm":
                    fallback = "Firm ground conditions provide good ball roll and predictable bounce, making approach shots easier"
                else:
                    fallback = "Mixed ground conditions mean some areas will be firmer than others, requiring adaptation throughout the round"
            elif busyness_rating in ["Quiet", "Moderate"] and "busy" not in [b.lower() for b in unique_bullets]:
                if busyness_rating == "Quiet":
                    fallback = "Quieter conditions today mean less waiting between shots, allowing you to maintain a good rhythm"
                else:
                    fallback = "Moderate course busyness today means some waiting is likely, but it shouldn't significantly disrupt your round"
            else:
                fallback = f"Given your handicap of {handicap}, today's conditions are well suited for your skill level"
        else:
            # Reference the worst available condition with explanation
            if not is_weather_favorable(weather_rating) and "weather" not in [b.lower() for b in unique_bullets]:
                if weather_rating == "Rainy":
                    fallback = "Rainy conditions today will affect ball flight, visibility, and overall comfort during your round"
                elif weather_rating == "Windy":
                    fallback = "Windy conditions today will significantly affect ball flight and distance control"
                elif weather_rating == "Cold":
                    fallback = "Cold conditions today will affect ball flight distance and make it harder to maintain flexibility"
                else:
                    fallback = "Poor weather conditions today will affect ball flight, visibility, and overall comfort during your round"
            elif ground_condition in ["Soft", "Soggy"] and "ground" not in [b.lower() for b in unique_bullets]:
                if ground_condition == "Soggy":
                    fallback = "Recent rain has left the ground very wet, which makes longer approaches and recovery shots harder"
                else:
                    fallback = "Soft ground conditions from recent rain make approach shots and recovery shots more difficult"
            elif busyness_rating in ["Very busy", "Busy"] and "busy" not in [b.lower() for b in unique_bullets]:
                fallback = f"Busier tee times today ({busyness_rating.lower()}) increase waiting between shots, which can affect your rhythm and enjoyment"
            else:
                fallback = f"Given your handicap of {handicap}, today's conditions are likely to add unnecessary difficulty to your round"
        
        if fallback.lower() not in seen_bullets:
            unique_bullets.append(fallback)
            seen_bullets.add(fallback.lower())
        else:
            # Prevent infinite loop - break if we can't add a new unique fallback
            break
    
    why_bullets_html = '<ul class="why-bullets">' + ''.join([f'<li>{bullet}</li>' for bullet in unique_bullets[:3]]) + '</ul>'
    
    # What to do instead - convert to bullets (always use bullets for better scanability)
    # Split by periods and filter out empty strings
    what_sentences = [s.strip() for s in what_to_do.split('.') if s.strip()]
    # If it's a single long sentence, try splitting by common separators
    if len(what_sentences) == 1 and len(what_to_do) > 80:
        # Try splitting by common patterns
        what_sentences = [s.strip() for s in what_to_do.replace('.', '|').split('|') if s.strip()]
    # Ensure we have 2-3 bullets max
    if len(what_sentences) > 3:
        what_sentences = what_sentences[:3]
    # Deduplicate bullets
    seen_what = set()
    unique_what_bullets = []
    for sentence in what_sentences:
        sentence_lower = sentence.lower()
        if sentence_lower not in seen_what:
            unique_what_bullets.append(sentence)
            seen_what.add(sentence_lower)
    # Always format as bullets
    if unique_what_bullets:
        what_bullets_html = '<ul class="what-bullets">' + ''.join([f'<li>{sentence}{"." if not sentence.endswith(".") else ""}</li>' for sentence in unique_what_bullets]) + '</ul>'
    else:
        what_bullets_html = f'<ul class="what-bullets"><li>{what_to_do}</li></ul>'
    
    # What section title
    what_section_title = "What to expect" if play_recommendation == "Play" else "What to do instead"
    
    # Verdict banner with status pill, course name and helper text
    verdict_banner_class = "play" if play_recommendation == "Play" else "dont-play"
    status_pill_text = "Play" if play_recommendation == "Play" else "Not ideal"
    verdict_banner_html = f"""
        <div class="verdict-banner {verdict_banner_class}">
            <div class="verdict-content">
                <div class="status-pill">{status_pill_text}</div>
                <div class="verdict-info">
                    <div class="verdict-course">{course}</div>
                    <div class="verdict-helper">{summary_first_sentence}</div>
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
                        <span>Suitability</span>
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
    
    # Debug info (only shown if debug_mode is True) - hidden from UI
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
            }}
            @media (max-width: 640px) {{
                .verdict-banner {{
                    padding: 12px 16px;
                }}
                .verdict-content {{
                    flex-direction: column;
                    gap: 10px;
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
                margin-bottom: 6px;
            }}
            .detail-label i {{
                width: 19px;
                height: 19px;
                opacity: 0.85;
                color: inherit;
            }}
            .detail-label span {{
                flex: 1;
            }}
            .detail-value {{
                color: var(--alba-cream);
                font-size: 16px;
                font-weight: 600;
                line-height: 1.4;
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
        </div>
        <script>
            (function() {{
                function initIcons() {{
                    if (typeof lucide !== 'undefined') {{
                        lucide.createIcons();
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
#             Handicap Suitability Borderline/Well suited, Recommendation depends on weather

