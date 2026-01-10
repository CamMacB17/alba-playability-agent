import json
import os
import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from urllib.parse import urlencode
import httpx
from typing import Dict, Any, Tuple
from uuid import uuid4

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
                # Validate that courses is a list with at least one course
                if isinstance(courses, list) and len(courses) > 0:
                    # Validate each course has required fields
                    required_fields = ["name", "lat", "lon", "popularity_tier", "difficulty", "beginner_friendly", "price_tier"]
                    if all(all(field in course for field in required_fields) for course in courses):
                        return courses
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


async def fetch_weather_data(lat: float, lon: float, target_date: str):
    """
    Fetch weather data for a specific date.
    Returns dict with temperature, wind_speed, precipitation, or None if error.
    """
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,wind_speed_10m_max,precipitation_sum",
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
                return {
                    "temperature_max": daily["temperature_2m_max"][0],
                    "temperature_min": daily["temperature_2m_min"][0],
                    "wind_speed": daily["wind_speed_10m_max"][0],
                    "precipitation": daily["precipitation_sum"][0]
                }
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


def determine_play_recommendation(weather_rating, ground_condition, busyness_rating, handicap_suitability):
    """
    Determine Play or Don't play recommendation.
    """
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


def generate_explanation_deterministic(weather_rating, ground_condition, busyness_rating, handicap_suitability, price_tier, tomorrow_forecast):
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
    
    parts.append(f"The price tier is {price_tier} (typical estimate).")
    
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
        - price_tier: str (£/££/£££)
        - verdict: str (Play/Don't play)
        - next_action: str
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
        "price_tier": assessment_data["price_tier"],
        "verdict": assessment_data["verdict"],
        "next_action": assessment_data["next_action"]
    }
    
    prompt = f"""Summarise this golf course playability assessment in one short paragraph using British English. 

You must only summarise the provided computed values. Do not invent facts, numbers, live prices, or live tee times. Do not use ampersands or em dashes.

Assessment data:
{json.dumps(structured_input, indent=2)}

Provide a concise paragraph that helps the golfer understand the conditions and suitability based solely on these computed ratings."""
    
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
                        "content": "You are a helpful assistant that summarises golf course playability assessments. Use British English. Be concise and factual. Only restate the provided computed values. Do not invent facts, numbers, live prices, or live tee times. Do not use ampersands or em dashes."
                    },
                    {"role": "user", "content": prompt}
                ],
                max_tokens=200,
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
    deterministic_explanation = generate_explanation_deterministic(
        assessment_data["weather_rating"],
        assessment_data["ground_rating"],
        assessment_data["busyness_rating"],
        assessment_data["suitability_rating"],
        assessment_data["price_tier"],
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
    courses = load_courses()
    
    # Generate course options HTML
    course_options = '<option value="">Select a course</option>\n'
    for course in courses:
        course_name = course["name"].replace('"', '&quot;')
        course_options += f'                    <option value="{course_name}">{course_name}</option>\n'
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Alba Labs</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                max-width: 600px;
                margin: 50px auto;
                padding: 20px;
                line-height: 1.6;
            }}
            h1 {{
                color: #333;
            }}
            form {{
                margin-top: 30px;
            }}
            .form-group {{
                margin-bottom: 20px;
            }}
            label {{
                display: block;
                margin-bottom: 5px;
                font-weight: bold;
            }}
            select, input[type="number"] {{
                width: 100%;
                padding: 8px;
                font-size: 14px;
                border: 1px solid #ccc;
                border-radius: 4px;
            }}
            .help-text {{
                font-size: 12px;
                color: #666;
                margin-top: 5px;
            }}
            button {{
                background-color: #007bff;
                color: white;
                padding: 10px 20px;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 16px;
            }}
            button:hover {{
                background-color: #0056b3;
            }}
            .build-footer {{
                font-size: 10px;
                color: #999;
                text-align: center;
                margin-top: 40px;
                padding-top: 20px;
                border-top: 1px solid #eee;
            }}
        </style>
    </head>
    <body>
        <h1>Alba Labs</h1>
        <form method="post" action="/assess">
            <div class="form-group">
                <label for="course">Course:</label>
                <select id="course" name="course" required>
{course_options}                </select>
            </div>
            
            <div class="form-group">
                <label for="handicap">Handicap:</label>
                <input type="number" id="handicap" name="handicap" min="0" max="54" value="25" required>
                <div class="help-text">Enter your handicap (0 to 54). Beginners typically start around 25-30.</div>
            </div>
            
            <div class="form-group">
                <label for="day">Day:</label>
                <select id="day" name="day" required>
                    <option value="Today">Today</option>
                    <option value="Tomorrow">Tomorrow</option>
                </select>
            </div>
            
            <div class="form-group">
                <label for="time_of_day">Time of day:</label>
                <select id="time_of_day" name="time_of_day" required>
                    <option value="Morning">Morning</option>
                    <option value="Midday">Midday</option>
                    <option value="Afternoon">Afternoon</option>
                    <option value="Evening">Evening</option>
                </select>
            </div>
            
            <button type="submit">Submit</button>
        </form>
        <div class="build-footer">Build: {BUILD_TIME_UTC}</div>
    </body>
    </html>
    """


async def render_assessment_results(course: str, handicap: int, day: str, time_of_day: str, force_llm: bool = False, llm_effective_enabled: bool = False, llm_raw=None, request_id: str = None):
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
    
    play_recommendation = determine_play_recommendation(
        weather_rating, ground_condition, busyness_rating, handicap_suitability
    )
    
    added_action = generate_added_action(
        play_recommendation, time_of_day, busyness_rating, weather_rating, handicap_suitability
    )
    
    # Set final_summary to deterministic by default
    final_summary = generate_explanation_deterministic(
        weather_rating,
        ground_condition,
        busyness_rating,
        handicap_suitability,
        course_data["price_tier"] if course_data else "££",
        tomorrow_forecast
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
            "price_tier": course_data["price_tier"] if course_data else "££",
            "verdict": play_recommendation,
            "next_action": added_action
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
            <div class="result-label">Weather Rating:</div>
            <div class="result-value">{weather_rating}</div>
        </div>
        <div class="result-item">
            <div class="result-label">Ground:</div>
            <div class="result-value">{ground_condition}</div>
        </div>
    """
    if tomorrow_forecast:
        weather_rating_html += f"""
        <div class="result-item">
            <div class="result-label">Tomorrow Forecast:</div>
            <div class="result-value">{tomorrow_forecast}</div>
        </div>
        """
    
    # 2. Busyness rating
    busyness_html = f"""
        <div class="result-item">
            <div class="result-label">Busyness Rating:</div>
            <div class="result-value">{busyness_rating}</div>
            <div class="help-text">Busyness estimate (not live tee times)</div>
        </div>
    """
    
    # 3. Handicap suitability
    handicap_html = f"""
        <div class="result-item">
            <div class="result-label">Handicap Suitability:</div>
            <div class="result-value">{handicap_suitability}</div>
        </div>
    """
    
    # 4. Price tier
    price_tier_display = course_data["price_tier"] if course_data else "££"
    price_html = f"""
        <div class="result-item">
            <div class="result-label">Price Tier:</div>
            <div class="result-value">{price_tier_display}</div>
            <div class="help-text">Typical estimate</div>
        </div>
    """
    
    # 5. Explanation paragraph
    # Determine mode badge text based on llm_effective_enabled
    if llm_effective_enabled:
        if summary_mode == "LLM":
            mode_badge_text = "Mode: LLM"
        else:
            mode_badge_text = "Mode: Deterministic (LLM failed)"
    else:
        mode_badge_text = "Mode: Deterministic"
    
    # Debug line (temporary) - show raw values
    llm_raw_str = str(llm_raw) if llm_raw is not None else "None"
    debug_line = f"llm_raw={llm_raw_str} llm_force={force_llm} effective={llm_effective_enabled}"
    
    explanation_html = f"""
        <div class="result-item">
            <div class="result-label">Summary: <span class="mode-badge">{mode_badge_text}</span></div>
            <div class="summary-mode">{debug_line}</div>
            <div class="result-value">{final_summary}</div>
        </div>
    """
    
    # 6. Play or Don't play
    play_html = f"""
        <div class="result-item" style="background-color: {'#d4edda' if play_recommendation == 'Play' else '#f8d7da'};">
            <div class="result-label" style="font-size: 18px;">Recommendation:</div>
            <div class="result-value" style="font-size: 20px; font-weight: bold;">{play_recommendation}</div>
        </div>
    """
    
    # 7. Added action
    action_html = f"""
        <div class="result-item">
            <div class="result-label">Suggestion:</div>
            <div class="result-value">{added_action}</div>
        </div>
    """
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Assessment Results - Alba Labs</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                max-width: 700px;
                margin: 50px auto;
                padding: 20px;
                line-height: 1.6;
            }}
            h1 {{
                color: #333;
            }}
            .result-item {{
                margin-bottom: 15px;
                padding: 15px;
                background-color: #f5f5f5;
                border-radius: 4px;
            }}
            .result-label {{
                font-weight: bold;
                color: #555;
                margin-bottom: 5px;
            }}
            .result-value {{
                color: #333;
            }}
            .help-text {{
                font-size: 12px;
                color: #666;
                margin-top: 5px;
                font-style: italic;
            }}
            .summary-mode {{
                font-size: 11px;
                color: #888;
                font-style: italic;
                margin-bottom: 8px;
            }}
            .mode-badge {{
                display: inline-block;
                font-size: 10px;
                padding: 2px 6px;
                background-color: #e9ecef;
                border: 1px solid #ced4da;
                border-radius: 3px;
                color: #495057;
                font-weight: normal;
                margin-left: 8px;
                font-style: normal;
            }}
            a {{
                display: inline-block;
                margin-top: 20px;
                color: #007bff;
                text-decoration: none;
            }}
            a:hover {{
                text-decoration: underline;
            }}
        </style>
    </head>
    <body>
        <h1>Assessment Results</h1>
        
        <div class="result-item">
            <div class="result-label">Course:</div>
            <div class="result-value">{course}</div>
        </div>
        
        {weather_rating_html}
        
        {busyness_html}
        
        {handicap_html}
        
        {price_html}
        
        {explanation_html}
        
        {play_html}
        
        {action_html}
        
        <a href="/">Back</a>
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
    llm: str = Query(None, description="Set to 1, '1', 'true', 'True', or 'yes' to force LLM summary")
):
    """
    Handle GET request for assessment results.
    """
    # Validate required parameters
    if not all([course, handicap is not None, day, time_of_day]):
        return RedirectResponse(url="/", status_code=303)
    
    # Parse llm parameter safely
    llm_raw = llm
    llm_force = parse_llm_parameter(llm)
    
    # Generate unique request ID for this request
    request_id = str(uuid4())
    
    # Compute llm_effective_enabled = has_openai_key and (llm_force or env_flag_true)
    has_openai_key = bool(OPENAI_API_KEY)
    llm_effective_enabled = has_openai_key and (llm_force or LLM_SUMMARY_ENABLED)
    
    # Log assessment start with debug info
    logger.info(f"ASSESS: request_id={request_id} llm_query={llm_raw} llm_flag={LLM_SUMMARY_ENABLED} has_key={has_openai_key} effective={llm_effective_enabled}")
    
    # Render results
    return await render_assessment_results(course, handicap, day, time_of_day, llm_force, llm_effective_enabled, llm_raw, request_id)


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

