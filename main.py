import json
import os
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from urllib.parse import urlencode
import httpx
from typing import Dict, Any

app = FastAPI()

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


async def generate_explanation_llm(assessment_data):
    """
    Generate explanation using OpenAI LLM with structured input.
    Falls back to deterministic if API call fails.
    
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
    """
    if not openai_client:
        return generate_explanation_deterministic(
            assessment_data["weather_rating"],
            assessment_data["ground_rating"],
            assessment_data["busyness_rating"],
            assessment_data["suitability_rating"],
            assessment_data["price_tier"],
            None  # tomorrow_forecast not in structured data
        )
    
    try:
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
        
        # Make async API call with timeout
        response = await asyncio.wait_for(
            openai_client.chat.completions.create(
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
        return explanation
        
    except (asyncio.TimeoutError, Exception):
        # Fall back to deterministic on any error (timeout, API error, etc.)
        return generate_explanation_deterministic(
            assessment_data["weather_rating"],
            assessment_data["ground_rating"],
            assessment_data["busyness_rating"],
            assessment_data["suitability_rating"],
            assessment_data["price_tier"],
            None
        )


async def generate_explanation(assessment_data, force_llm=False):
    """
    Generate explanation paragraph. Uses LLM if enabled and available, otherwise uses deterministic version.
    
    assessment_data: dict containing all required fields for structured LLM input
    force_llm: if True, force LLM usage (if API key available)
    """
    # Check if LLM should be used
    use_llm = False
    if force_llm:
        # Force LLM if API key is available
        if openai_client:
            use_llm = True
        else:
            # Return special message if LLM requested but unavailable
            return "LLM summary unavailable on this deployment."
    elif LLM_SUMMARY_ENABLED and openai_client:
        use_llm = True
    
    if use_llm:
        return await generate_explanation_llm(assessment_data)
    else:
        return generate_explanation_deterministic(
            assessment_data["weather_rating"],
            assessment_data["ground_rating"],
            assessment_data["busyness_rating"],
            assessment_data["suitability_rating"],
            assessment_data["price_tier"],
            None  # tomorrow_forecast not needed for deterministic
        )


@app.get("/debug/env")
async def debug_env() -> Dict[str, Any]:
    """
    Debug endpoint to check environment variables.
    Returns JSON with OpenAI API key status and LLM_SUMMARY flag value.
    """
    return {
        "has_openai_key": bool(OPENAI_API_KEY),
        "llm_summary_flag": os.getenv("LLM_SUMMARY", "")
    }


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
    </body>
    </html>
    """


async def render_assessment_results(course: str, handicap: int, day: str, time_of_day: str, force_llm: bool = False):
    """
    Shared function to calculate ratings and render assessment results.
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
    
    explanation = await generate_explanation(assessment_data, force_llm=force_llm)
    
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
    explanation_html = f"""
        <div class="result-item">
            <div class="result-label">Summary:</div>
            <div class="result-value">{explanation}</div>
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


@app.get("/assess", response_class=HTMLResponse)
async def assess_get(
    course: str = Query(None),
    handicap: int = Query(None),
    day: str = Query(None),
    time_of_day: str = Query(None),
    llm: int = Query(0, description="Set to 1 to force LLM summary")
):
    """
    Handle GET request for assessment results.
    """
    # Validate required parameters
    if not all([course, handicap is not None, day, time_of_day]):
        return RedirectResponse(url="/", status_code=303)
    
    # Render results
    force_llm = llm == 1
    return await render_assessment_results(course, handicap, day, time_of_day, force_llm)


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

