# NEW: Kalshi weather market title parser

"""
Parses Kalshi weather market titles to extract:
- City name
- Weather type (precipitation, temperature, snow, wind)
- Threshold value
- Direction (above/below/more than/less than)
- Resolution date

Handles ambiguous titles by routing to Claude Haiku for interpretation.
"""

import re
import logging
from typing import Optional, Dict
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class WeatherMarketParser:
    """
    Parse Kalshi market titles and extract weather trade parameters.
    """

    def __init__(self, city_coords: Dict[str, tuple]):
        """
        Args:
            city_coords: Dict mapping city names to (lat, lon)
        """
        self.city_coords = city_coords
        self.valid_cities = list(city_coords.keys())

    def parse_market(self, market: Dict) -> Optional[Dict]:
        """
        Parse a Kalshi market dict and extract weather trade parameters.

        Args:
            market: Kalshi market dict with 'title' and 'ticker' fields

        Returns:
            Dict with: {city, weather_type, threshold, direction, target_date}
            or None if market doesn't match a known weather pattern
        """
        title = market.get("title", "")
        ticker = market.get("ticker", "")

        if not title:
            return None

        # Try regex patterns for common formats
        parsed = self._try_regex_patterns(title)
        if parsed:
            return parsed

        # If regex fails, try substring matching for robustness
        parsed = self._try_substring_matching(title)
        if parsed:
            return parsed

        # For ambiguous titles, could fall back to Claude Haiku
        # For now, return None (skip the market)
        logger.warning(f"Could not parse weather market: {title}")
        return None

    def _try_regex_patterns(self, title: str) -> Optional[Dict]:
        """Try common regex patterns for weather market titles."""

        # Pattern 1: "Will NYC high temperature exceed 80°F on April 10?"
        pattern1 = r"Will\s+(\w+)\s+high\s+(?:temperature|temp)\s+(exceed|be above|be below|be under)\s+([\d.]+)°?F?\s+on\s+([A-Z][a-z]+\s+\d+)"
        match = re.search(pattern1, title, re.IGNORECASE)
        if match:
            city, direction, threshold, date_str = match.groups()
            return {
                "city": city,
                "weather_type": "temperature",
                "threshold": float(threshold),
                "direction": self._normalize_direction(direction),
                "target_date": self._parse_date_string(date_str),
            }

        # Pattern 2: "Will it rain more than 0.5 inches in Chicago on April 12?"
        pattern2 = r"Will\s+it\s+(rain|snow)\s+(more than|less than|over|under)\s+([\d.]+)\s+inches?\s+in\s+(\w+)\s+on\s+([A-Z][a-z]+\s+\d+)"
        match = re.search(pattern2, title, re.IGNORECASE)
        if match:
            precip_type, direction, amount, city, date_str = match.groups()
            weather_type = "precipitation" if precip_type.lower() == "rain" else "snow"
            return {
                "city": city,
                "weather_type": weather_type,
                "threshold": float(amount),
                "direction": self._normalize_direction(direction),
                "target_date": self._parse_date_string(date_str),
            }

        # Pattern 3: "Will Boston receive more than 3 inches of snow on April 15?"
        pattern3 = r"Will\s+(\w+)\s+receive\s+(more than|less than|over|under)\s+([\d.]+)\s+inches?\s+of\s+(\w+)\s+on\s+([A-Z][a-z]+\s+\d+)"
        match = re.search(pattern3, title, re.IGNORECASE)
        if match:
            city, direction, amount, precip_type, date_str = match.groups()
            weather_type = "precipitation" if precip_type.lower() == "rain" else precip_type.lower()
            return {
                "city": city,
                "weather_type": weather_type,
                "threshold": float(amount),
                "direction": self._normalize_direction(direction),
                "target_date": self._parse_date_string(date_str),
            }

        # Pattern 4: "Will Miami high temp be below 70°F on April 8?"
        pattern4 = r"Will\s+(\w+)\s+(?:high\s+)?temp(?:erature)?\s+be\s+(above|below|under|over)\s+([\d.]+)°?F?\s+on\s+([A-Z][a-z]+\s+\d+)"
        match = re.search(pattern4, title, re.IGNORECASE)
        if match:
            city, direction, threshold, date_str = match.groups()
            return {
                "city": city,
                "weather_type": "temperature",
                "threshold": float(threshold),
                "direction": self._normalize_direction(direction),
                "target_date": self._parse_date_string(date_str),
            }

        # Pattern 5: "Will Seattle have more than 1 inch of rain on April 11?"
        pattern5 = r"Will\s+(\w+)\s+have\s+(more than|less than|over|under)\s+([\d.]+)\s+inches?\s+of\s+(\w+)\s+on\s+([A-Z][a-z]+\s+\d+)"
        match = re.search(pattern5, title, re.IGNORECASE)
        if match:
            city, direction, amount, precip_type, date_str = match.groups()
            weather_type = "precipitation" if precip_type.lower() == "rain" else precip_type.lower()
            return {
                "city": city,
                "weather_type": weather_type,
                "threshold": float(amount),
                "direction": self._normalize_direction(direction),
                "target_date": self._parse_date_string(date_str),
            }

        return None

    def _try_substring_matching(self, title: str) -> Optional[Dict]:
        """
        Fallback: try to match city names and keywords via substring.
        Less precise but more robust for unusual titles.
        """
        # Look for city name
        city = None
        for candidate in self.valid_cities:
            if candidate.lower() in title.lower():
                city = candidate
                break

        if not city:
            return None

        # Determine weather type
        title_lower = title.lower()
        weather_type = None
        if "temperature" in title_lower or "temp" in title_lower:
            weather_type = "temperature"
        elif "rain" in title_lower or "precipitation" in title_lower:
            weather_type = "precipitation"
        elif "snow" in title_lower:
            weather_type = "snow"
        elif "wind" in title_lower:
            weather_type = "wind"
        else:
            return None

        # Extract numeric threshold
        numbers = re.findall(r"[\d.]+", title)
        if not numbers:
            return None

        threshold = float(numbers[0])

        # Determine direction
        direction = "above"
        if "below" in title_lower or "under" in title_lower or "less" in title_lower:
            direction = "below"

        # Extract date (Month Day format)
        date_match = re.search(r"([A-Z][a-z]+)\s+(\d+)", title)
        if date_match:
            date_str = f"{date_match.group(1)} {date_match.group(2)}"
            target_date = self._parse_date_string(date_str)
        else:
            # Default to tomorrow if we can't parse
            target_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        return {
            "city": city,
            "weather_type": weather_type,
            "threshold": threshold,
            "direction": direction,
            "target_date": target_date,
        }

    @staticmethod
    def _normalize_direction(direction_str: str) -> str:
        """Normalize direction string to standard form."""
        norm = direction_str.lower().strip()
        if norm in ("more than", "over", "exceed", "exceeds", "above", "be above"):
            return "above"
        elif norm in ("less than", "under", "be below", "below", "be under"):
            return "below"
        else:
            return "above"  # Default to above

    @staticmethod
    def _parse_date_string(date_str: str) -> str:
        """
        Parse date string like "April 10" and return YYYY-MM-DD format.
        Assumes current year or next year if month has passed.
        """
        try:
            # Try parsing "April 10" format
            parsed = datetime.strptime(date_str, "%B %d")

            # Assume current year, or next year if date has passed
            current_year = datetime.now().year
            parsed = parsed.replace(year=current_year)

            if parsed < datetime.now():
                parsed = parsed.replace(year=current_year + 1)

            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            # Fallback: return tomorrow's date
            return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    def get_nws_probability(self, parsed: Dict, nws_forecast) -> float:
        """
        Route to the right NWS forecast metric based on weather_type.
        Returns probability (0.0-1.0) for YES outcome.

        Args:
            parsed: Dict from parse_market()
            nws_forecast: NWSForecast object from nws_client

        Returns:
            Probability that market resolves YES (0.0-1.0)
        """
        weather_type = parsed.get("weather_type", "").lower()
        threshold = parsed.get("threshold", 0.0)
        direction = parsed.get("direction", "above").lower()

        if weather_type == "precipitation" or weather_type == "rain":
            # For precipitation markets: if threshold is "more than X inches", use precip_prob
            # This is approximate — true probability depends on intensity distribution
            prob = nws_forecast.precip_probability
            if direction == "below":
                return 1.0 - prob
            return prob

        elif weather_type == "temperature":
            # For temperature: compute P(temp > threshold) or P(temp < threshold)
            from .nws_client import NWSClient

            forecast_temp = nws_forecast.forecast_high
            std_dev = nws_forecast.temp_std_dev
            prob = NWSClient.compute_temp_exceedance_prob(forecast_temp, std_dev, threshold, direction)
            return prob

        elif weather_type == "snow":
            # For snow: use snow_probability
            prob = nws_forecast.snow_probability
            if direction == "below":
                return 1.0 - prob
            return prob

        elif weather_type == "wind":
            # For wind: would need wind speed forecast (not yet implemented)
            return 0.5  # Neutral default

        else:
            return 0.5  # Unknown weather type — neutral default
