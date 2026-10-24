"""Weather MCP server.

Exposes two tools over MCP:

* ``get_current_weather`` - current conditions for a city
* ``get_forecast``        - the next few days for a city

OpenWeather is used when ``OPENWEATHER_API_KEY`` holds a real key. When the
key is missing, still a placeholder, or the API rejects it, the tools fall
back to Open-Meteo (no key required) so a broken/missing key never takes
the weather agent down. Both providers are normalised to the same shape.
"""

from __future__ import annotations

import os

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("Weather MCP Server")

OPENWEATHER_API_KEY = (os.getenv("OPENWEATHER_API_KEY") or "").strip()
OPENWEATHER_BASE = "https://api.openweathermap.org/data/2.5"
OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_SECONDS = 15

# WMO weather interpretation codes used by Open-Meteo.
WMO_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def _has_openweather_key() -> bool:
    key = OPENWEATHER_API_KEY
    if not key:
        return False
    lowered = key.lower()
    return not (
        lowered.startswith("your_")
        or lowered in {"changeme", "replace_me", "none", "null"}
    )


def _describe(code) -> str:
    try:
        return WMO_CODES.get(int(code), "unknown conditions")
    except (TypeError, ValueError):
        return "unknown conditions"


def _get_json(url: str, params: dict) -> dict | None:
    """GET ``url`` and return JSON, or None when the request failed."""
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ----------------------------------------------------------------------
# OpenWeather
# ----------------------------------------------------------------------

def _openweather_current(city: str) -> dict | None:
    if not _has_openweather_key():
        return None
    data = _get_json(
        f"{OPENWEATHER_BASE}/weather",
        {"q": city, "appid": OPENWEATHER_API_KEY, "units": "metric"},
    )
    if not data or "main" not in data or "weather" not in data:
        return None
    return data


def _openweather_forecast(city: str) -> dict | None:
    if not _has_openweather_key():
        return None
    data = _get_json(
        f"{OPENWEATHER_BASE}/forecast",
        {"q": city, "appid": OPENWEATHER_API_KEY, "units": "metric"},
    )
    if not data or not isinstance(data.get("list"), list):
        return None
    return data


# ----------------------------------------------------------------------
# Open-Meteo fallback (no API key)
# ----------------------------------------------------------------------

def _geocode(city: str) -> dict | None:
    data = _get_json(
        OPEN_METEO_GEOCODE,
        {"name": city, "count": 1, "language": "en", "format": "json"},
    )
    results = (data or {}).get("results") or []
    return results[0] if results else None


def _openmeteo(city: str, days: int = 5) -> tuple[dict, dict] | None:
    place = _geocode(city)
    if not place:
        return None
    data = _get_json(
        OPEN_METEO_FORECAST,
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": (
                "temperature_2m,apparent_temperature,relative_humidity_2m,"
                "weather_code,wind_speed_10m"
            ),
            "daily": "temperature_2m_min,temperature_2m_max,weather_code",
            "timezone": "auto",
            "forecast_days": max(1, min(int(days or 5), 16)),
        },
    )
    if not data:
        return None
    return place, data


def _openmeteo_current(city: str) -> dict | None:
    result = _openmeteo(city, days=1)
    if not result:
        return None
    place, data = result
    current = data.get("current") or {}
    return {
        "city": place.get("name") or city,
        "temperature_c": current.get("temperature_2m"),
        "feels_like_c": current.get("apparent_temperature"),
        "humidity": current.get("relative_humidity_2m"),
        "condition": _describe(current.get("weather_code")),
        "wind_speed": current.get("wind_speed_10m"),
        "wind_speed_unit": "km/h",
        "source": "open-meteo",
    }


def _openmeteo_forecast(city: str) -> dict | None:
    result = _openmeteo(city, days=5)
    if not result:
        return None
    place, data = result
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    lows = daily.get("temperature_2m_min") or []
    highs = daily.get("temperature_2m_max") or []
    codes = daily.get("weather_code") or []

    forecast = []
    for index, date in enumerate(dates[:5]):
        low = lows[index] if index < len(lows) else None
        high = highs[index] if index < len(highs) else None
        average = None
        if low is not None and high is not None:
            average = round((float(low) + float(high)) / 2.0, 1)
        forecast.append(
            {
                "datetime": date,
                "temperature": average,
                "temperature_min": low,
                "temperature_max": high,
                "weather": _describe(codes[index] if index < len(codes) else None),
            }
        )
    return {
        "city": place.get("name") or city,
        "source": "open-meteo",
        "forecast": forecast,
    }


# ----------------------------------------------------------------------
# MCP tools
# ----------------------------------------------------------------------

@mcp.tool()
def get_current_weather(city: str):
    """Current weather for ``city`` (OpenWeather, falling back to Open-Meteo)."""
    data = _openweather_current(city)
    if data:
        try:
            return {
                "city": data["name"],
                "temperature_c": data["main"]["temp"],
                "feels_like_c": data["main"]["feels_like"],
                "humidity": data["main"]["humidity"],
                "condition": data["weather"][0]["description"],
                "wind_speed": data["wind"]["speed"],
                "wind_speed_unit": "m/s",
                "source": "openweather",
            }
        except (KeyError, IndexError, TypeError):
            pass

    fallback = _openmeteo_current(city)
    if fallback:
        return fallback

    return {
        "city": city,
        "error": (
            "Weather is unavailable: the OpenWeather key was rejected or is "
            "missing, and the Open-Meteo fallback could not be reached."
        ),
    }


@mcp.tool()
def get_forecast(city: str):
    """Five-day forecast for ``city`` (OpenWeather, falling back to Open-Meteo)."""
    data = _openweather_forecast(city)
    if data:
        forecast = []
        for item in data["list"][:5]:
            try:
                forecast.append(
                    {
                        "datetime": item["dt_txt"],
                        "temperature": item["main"]["temp"],
                        "weather": item["weather"][0]["description"],
                    }
                )
            except (KeyError, IndexError, TypeError):
                continue
        if forecast:
            return {
                "city": data.get("city", {}).get("name") or city,
                "source": "openweather",
                "forecast": forecast,
            }

    fallback = _openmeteo_forecast(city)
    if fallback:
        return fallback

    return {
        "city": city,
        "error": (
            "Forecast is unavailable: the OpenWeather key was rejected or is "
            "missing, and the Open-Meteo fallback could not be reached."
        ),
        "forecast": [],
    }


if __name__ == "__main__":
    mcp.run()
