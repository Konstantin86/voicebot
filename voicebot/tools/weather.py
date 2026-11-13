"""weather tool using Open-Meteo (free, no API key)."""

import logging
import re
import time
from typing import Any

import aiohttp

from . import Tool

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_SECONDS = 8
HOURLY_STEP = 3

# WMO weather codes used by Open-Meteo.
WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}

DESCRIPTION = (
    "Current weather and forecast (up to 7 days) for a city. Leave location empty for the user's home. "
    "Use for weather, temperature, rain, snow, wind, what to wear, sunrise or sunset questions."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "location": {"type": "string", "description": "City name in English, e.g. 'Lviv'. Empty = home."},
        "days": {"type": "integer", "description": "Forecast days, 1 to 7. Default 2 (today and tomorrow)."},
    },
}


def home_from_context(context_file: str) -> str:
    """City from a 'Location: Kharkiv, Ukraine.' line in context.md, or ''."""
    try:
        with open(context_file, encoding="utf-8") as f:
            m = re.search(r"Location:\s*([^,.\n]+)", f.read())
    except OSError:
        return ""
    return m.group(1).strip() if m else ""


def describe(code: Any) -> str:
    return WEATHER_CODES.get(code, f"weather code {code}")


def summarize(place: str, data: dict) -> dict[str, Any]:
    current = data.get("current", {})
    hourly = data.get("hourly", {})
    daily = data.get("daily", {})
    hours = [
        {
            "time": t[11:16],
            "temp_c": hourly["temperature_2m"][i],
            "rain_chance_pct": hourly["precipitation_probability"][i],
            "sky": describe(hourly["weather_code"][i]),
        }
        for i, t in enumerate(hourly.get("time", []))
        if i % HOURLY_STEP == 0
    ]
    days = [
        {
            "date": d,
            "sky": describe(daily["weather_code"][i]),
            "min_c": daily["temperature_2m_min"][i],
            "max_c": daily["temperature_2m_max"][i],
            "rain_chance_pct": daily["precipitation_probability_max"][i],
            "precipitation_mm": daily["precipitation_sum"][i],
            "max_wind_m_s": daily["wind_speed_10m_max"][i],
            "sunrise": daily["sunrise"][i][11:],
            "sunset": daily["sunset"][i][11:],
        }
        for i, d in enumerate(daily.get("time", []))
    ]
    return {
        "place": place,
        "now": {
            "time": (current.get("time") or "")[11:],
            "temp_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "sky": describe(current.get("weather_code")),
            "wind_m_s": current.get("wind_speed_10m"),
            "humidity_pct": current.get("relative_humidity_2m"),
        },
        "next_hours": hours,
        "days": days,
        "note": "Round numbers when speaking. Mention only what the user asked about.",
    }


def make_tool(home: str) -> Tool:
    places: dict[str, tuple[float, float, str]] = {}

    async def geocode(http: aiohttp.ClientSession, name: str) -> tuple[float, float, str] | None:
        key = name.lower()
        if key not in places:
            params = {"name": name, "count": 1, "language": "en", "format": "json"}
            async with http.get(GEOCODE_URL, params=params) as resp:
                resp.raise_for_status()
                results = (await resp.json()).get("results") or []
            if not results:
                return None
            r = results[0]
            places[key] = (r["latitude"], r["longitude"], ", ".join(filter(None, [r.get("name"), r.get("country")])))
        return places[key]

    async def weather(location: str = "", days: int = 2) -> dict[str, Any]:
        name = (location or "").strip() or home
        if not name:
            return {"error": "No location given and no home location set. Ask the user which city."}
        days = max(1, min(7, int(days or 2)))
        started = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as http:
                place = await geocode(http, name)
                if place is None:
                    return {"error": f"Unknown place {name!r}. Ask the user to say the city name again."}
                lat, lon, label = place
                params = {
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
                    "hourly": "temperature_2m,precipitation_probability,weather_code",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                    "precipitation_sum,wind_speed_10m_max,sunrise,sunset",
                    "timezone": "auto",
                    "forecast_days": days,
                    "forecast_hours": 24,
                    "wind_speed_unit": "ms",
                }
                async with http.get(FORECAST_URL, params=params) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("weather: %s", e)
            return {"error": "Weather service did not respond."}
        result = summarize(label, data)
        log.info(
            "weather %s (%d days) in %.1f s: now %s °C, %s",
            label, days, time.monotonic() - started, result["now"]["temp_c"], result["now"]["sky"],
        )
        return result

    return Tool(name="weather", description=DESCRIPTION, parameters=PARAMETERS, handler=weather)
