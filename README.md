# Alba Playability Agent

A FastAPI application that assesses golf course playability based on weather conditions, course characteristics, and player handicap.

## Features

- Live weather integration using Open-Meteo API
- Course playability ratings (weather, busyness, handicap suitability)
- Ground condition assessment based on historical rainfall
- Play/Don't play recommendations with actionable suggestions

## Quick Start

### Using Docker (Recommended)

1. Build and start the container:
   ```bash
   docker compose up --build
   ```

2. View the app locally:
   Open your browser to `http://localhost:8000`

3. To stop the container:
   ```bash
   docker compose down
   ```

### Using Python Directly

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Start the server:
   ```bash
   python main.py
   ```

3. Open your browser to `http://localhost:8000`

## Course Enrichment

The `enrich_courses.py` script adds confidence fields to course data, allowing you to track how reliable each course tag is.

### How to Run Enrichment

1. Ensure you have `courses.json` in the project directory

2. (Optional) Create `overrides.json` for manual corrections (see below)

3. Run the enrichment script:
   ```bash
   python enrich_courses.py
   ```

   This will create `courses_enriched.json` with confidence fields added.

4. The script supports command-line options:
   ```bash
   python enrich_courses.py --input courses.json --output courses_enriched.json --overrides overrides.json
   ```

### How to Swap in courses_enriched.json

To use the enriched courses in the application:

1. **Option 1: Replace courses.json** (if you want to use enriched data permanently):
   ```bash
   cp courses_enriched.json courses.json
   ```

2. **Option 2: Update main.py** to load `courses_enriched.json`:
   ```python
   courses_file = os.path.join(os.path.dirname(__file__), "courses_enriched.json")
   ```

   Note: The current implementation loads `courses.json` by default. If you want to use enriched courses, you'll need to update the `load_courses()` function in `main.py` or replace `courses.json` with the enriched version.

### Manual Overrides

Create `overrides.json` to lock specific values for courses. This is useful for quick corrections without modifying the main courses file.

Example `overrides.json`:
```json
{
  "Trent Park Golf Club": {
    "popularity_tier": "Medium",
    "popularity_confidence": "High",
    "difficulty": "Medium",
    "difficulty_confidence": "High"
  },
  "Red Libbets Golf Club": {
    "price_tier": "£",
    "price_confidence": "Medium"
  }
}
```

The overrides file uses course names as keys. You can override:
- Any existing course field (name, lat, lon, popularity_tier, difficulty, beginner_friendly, price_tier)
- Confidence fields (popularity_confidence, difficulty_confidence, beginner_confidence, price_confidence)

Confidence values must be: `High`, `Medium`, or `Low`.

### How to Add New Courses Safely

1. **Add to courses.json**:
   ```json
   {
     "name": "New Course Name",
     "lat": 51.5074,
     "lon": -0.1278,
     "popularity_tier": "Medium",
     "difficulty": "Medium",
     "beginner_friendly": "Mixed",
     "price_tier": "££"
   }
   ```

2. **Run enrichment** to add confidence fields:
   ```bash
   python enrich_courses.py
   ```

3. **Test the new course** by submitting the form on the homepage

4. **If corrections needed**, add to `overrides.json`:
   ```json
   {
     "New Course Name": {
       "difficulty": "Easy",
       "difficulty_confidence": "High"
     }
   }
   ```

5. **Re-run enrichment** to apply overrides:
   ```bash
   python enrich_courses.py
   ```

### Enrichment Behaviour

- **Default confidence**: If web access is unavailable or enrichment cannot improve confidence, all confidence fields default to `Medium`
- **Existing tags preserved**: The script does not modify existing course tags unless overridden
- **No live data scraping**: The script does NOT scrape live tee times, booking pages, or exact prices
- **Price tiers**: All price tiers are rough estimates only

### Confidence Fields

Each course in `courses_enriched.json` will have these additional fields:
- `popularity_confidence`: High / Medium / Low
- `difficulty_confidence`: High / Medium / Low
- `beginner_confidence`: High / Medium / Low
- `price_confidence`: High / Medium / Low

## Requirements

See `requirements.txt` for Python dependencies.

## Notes

- Weather data is fetched from Open-Meteo (no API key required)
- The application handles API failures gracefully
- All text uses British English spelling
- Price tiers are estimates only and should not be used for exact pricing

