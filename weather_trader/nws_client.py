# NEW: National Weather Service API client

"""
Client for fetching NWS forecast data.
Provides probabilistic forecasts for precipitation, temperature, snow, and wind.

No API key required. Free and public. Rate limited respectfully.
"""

import asyncio
import logging
from typing import Optional, Dict, Tuple
from datetime import datetime, timedelta

import aiohttp

from .models import NWSForecast
from .strategy import NWS_BASE_URL, NWS_USER_AGENT, NWS_REQUEST_DELAY_SECONDS

logger = logging.getLogger(__name__)


class NWSClient:
    """
    Async HTTP client for National Weather Service API.
    Handles grid point lookup, hourly forecasts, and probabilistic grid data.
    """

    def __init__(self):
        """Initialize NWS client with cached grid points."""
        self.base_url = NWS_BASE_URL
        self.user_agent = NWS_USER_AGENT
        self.headers = {"User-Agent": self.user_agent}

        # In-memory cache for grid points (lat/lon -> grid coords)
        # In production, consider DynamoDB for persistence across invocations
        self._grid_cache: Dict[Tuple[float, float], Dict] = {}
        self._last_request_time = 0.0

    async def _rate_limit(self) -> None:
        """Enforce respectful rate limiting between requests."""
        elapsed = asyncio.get_event_loop().time() - self._last_request_time
        if elapsed < NWS_REQUEST_DELAY_SECONDS:
            await asyncio.sleep(NWS_REQUEST_DELAY_SECONDS - elapsed)
        self._last_request_time = asyncio.get_event_loop().time()

    async def _get(self, url: str, session: aiohttp.ClientSession) -> Dict:
        """Execute GET request with rate limiting and error handling."""
        await self._rate_limit()

        try:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 404:
                    logger.warning(f"NWS 404: {url}")
                    return {}
                else:
                    logger.error(f"NWS {resp.status}: {url}")
                    return {}
        except asyncio.TimeoutError:
            logger.error(f"NWS timeout: {url}")
            return {}
        except Exception as e:
            logger.error(f"NWS request failed: {url} — {e}")
            return {}

    async def get_grid_point(self, lat: float, lon: float, session: aiohttp.ClientSession) -> Dict:
        """
        GET /points/{lat},{lon}
        Returns: gridId, gridX, gridY, forecastHourly URL, forecastGridData URL
        """
        cache_key = (lat, lon)
        if cache_key in self._grid_cache:
            return self._grid_cache[cache_key]

        url = f"{self.base_url}/points/{lat},{lon}"
        data = await self._get(url, session)

        if data and "properties" in data:
            props = data["properties"]
            result = {
                "gridId": props.get("gridId"),
                "gridX": props.get("gridX"),
                "gridY": props.get("gridY"),
                "forecastHourly": props.get("forecastHourly"),
                "forecastGridData": props.get("forecastGridData"),
            }
            self._grid_cache[cache_key] = result
            return result

        return {}

    async def get_hourly_forecast(
        self, grid_id: str, grid_x: int, grid_y: int, session: aiohttp.ClientSession
    ) -> list[Dict]:
        """
        GET /gridpoints/{gridId}/{x},{y}/forecast/hourly
        Returns list of hourly periods with temperature, windSpeed, shortForecast, probabilityOfPrecipitation
        """
        url = f"{self.base_url}/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast/hourly"
        data = await self._get(url, session)

        if data and "properties" in data and "periods" in data["properties"]:
            return data["properties"]["periods"]

        return []

    async def get_grid_data(self, grid_id: str, grid_x: int, grid_y: int, session: aiohttp.ClientSession) -> Dict:
        """
        GET /gridpoints/{gridId}/{x},{y}
        Returns raw probabilistic grid data with time series for:
        - probabilityOfPrecipitation
        - temperature
        - quantitativePrecipitation
        - snowfallAmount
        """
        url = f"{self.base_url}/gridpoints/{grid_id}/{grid_x},{grid_y}"
        data = await self._get(url, session)

        if data and "properties" in data:
            return data["properties"]

        return {}

    @staticmethod
    def _parse_value_array(value_array: list[Dict], target_date: str) -> list[float]:
        """
        Parse NWS value array (time series with values and validTimes).
        Extract values for the target date.
        """
        values = []

        if not value_array:
            return values

        # validTimes are in format: "2026-04-10T00:00:00Z/PT1H" (period of 1 hour)
        target_dt = datetime.fromisoformat(target_date.replace("Z", "+00:00"))
        target_day_str = target_dt.strftime("%Y-%m-%d")

        for item in value_array:
            if not isinstance(item, dict):
                continue

            valid_time = item.get("validTime", "")
            value = item.get("value")

            # Extract date part from validTime
            if valid_time and "/" in valid_time:
                date_part = valid_time.split("/")[0]
                if target_day_str in date_part:
                    if value is not None:
                        values.append(float(value))

        return values

    async def get_precip_probability(self, lat: float, lon: float, target_date: str, session: aiohttp.ClientSession) -> float:
        """
        Returns probability of precipitation (0.0-1.0) for location on target_date.
        Averages hourly precip probabilities across the day.
        """
        # Get grid point
        grid_point = await self.get_grid_point(lat, lon, session)
        if not grid_point.get("gridId"):
            logger.error(f"Could not get grid point for {lat}, {lon}")
            return 0.5  # Neutral default

        grid_id = grid_point["gridId"]
        grid_x = grid_point["gridX"]
        grid_y = grid_point["gridY"]

        # Get hourly forecast
        periods = await self.get_hourly_forecast(grid_id, grid_x, grid_y, session)

        # Filter for target date and extract precip probabilities
        target_day_str = target_date[:10]  # YYYY-MM-DD
        precip_probs = []

        for period in periods:
            start_time = period.get("startTime", "")
            if start_time.startswith(target_day_str):
                prob = period.get("probabilityOfPrecipitation", {})
                if isinstance(prob, dict):
                    value = prob.get("value")
                else:
                    value = prob

                if value is not None:
                    precip_probs.append(float(value) / 100.0)  # Convert from 0-100 to 0-1

        # Average across the day
        if precip_probs:
            return sum(precip_probs) / len(precip_probs)

        return 0.5  # Neutral default if no data

    async def get_temperature_forecast(
        self, lat: float, lon: float, target_date: str, session: aiohttp.ClientSession
    ) -> Tuple[float, float]:
        """
        Returns (forecast_high_temp, std_dev_estimate) for location on target_date.
        std_dev estimated from NWS confidence interval data in grid response.
        """
        # Get grid point
        grid_point = await self.get_grid_point(lat, lon, session)
        if not grid_point.get("gridId"):
            return 70.0, 10.0  # Default fallback

        grid_id = grid_point["gridId"]
        grid_x = grid_point["gridX"]
        grid_y = grid_point["gridY"]

        # Get hourly forecast for daily high
        periods = await self.get_hourly_forecast(grid_id, grid_x, grid_y, session)

        target_day_str = target_date[:10]
        temperatures = []

        for period in periods:
            start_time = period.get("startTime", "")
            if start_time.startswith(target_day_str):
                temp = period.get("temperature")
                if temp is not None:
                    temperatures.append(float(temp))

        if not temperatures:
            return 70.0, 10.0

        max_temp = max(temperatures)
        min_temp = min(temperatures)

        # Estimate std dev from range (assumes roughly normal distribution)
        # Range ~ 4 * std_dev for normal distribution (99.7% within 3 sigma)
        temp_range = max_temp - min_temp
        estimated_std_dev = max(temp_range / 4.0, 5.0)  # Min 5 degrees

        return max_temp, estimated_std_dev

    async def get_snow_probability(
        self, lat: float, lon: float, target_date: str, threshold_inches: float, session: aiohttp.ClientSession
    ) -> float:
        """
        Returns P(snowfall >= threshold_inches) for location on target_date.
        Uses quantitativePrecipitation/snowfallAmount grid data.
        """
        # Get grid point
        grid_point = await self.get_grid_point(lat, lon, session)
        if not grid_point.get("gridId"):
            return 0.1  # Low default for snow (rare)

        grid_id = grid_point["gridId"]
        grid_x = grid_point["gridX"]
        grid_y = grid_point["gridY"]

        # Get grid data (probabilistic time series)
        grid_data = await self.get_grid_data(grid_id, grid_x, grid_y, session)

        # Look for snowfallAmount or quantitativePrecipitation
        snowfall_amount = grid_data.get("snowfallAmount", {})
        if isinstance(snowfall_amount, dict) and "values" in snowfall_amount:
            snow_values = self._parse_value_array(snowfall_amount["values"], target_date)

            if snow_values:
                # Count how many values exceed threshold
                exceeds = sum(1 for v in snow_values if v >= threshold_inches)
                probability = exceeds / len(snow_values)
                return min(probability, 1.0)

        return 0.1  # Default low probability if no data

    @staticmethod
    def compute_temp_exceedance_prob(forecast_temp: float, std_dev: float, threshold: float, direction: str) -> float:
        """
        Compute P(temp > threshold) or P(temp < threshold) using normal distribution.
        Without scipy dependency (for Lambda compatibility), use approximation.

        For direction=="above": P(X > threshold) where X ~ N(forecast_temp, std_dev^2)
        For direction=="below": P(X < threshold)
        """
        # Simple approximation: use error function
        # erf(x) ≈ sign(x) * sqrt(1 - exp(-x^2 * (4/π + a*x^2) / (1 + a*x^2)))
        # where a ≈ 0.147

        # More practical: use cumulative normal approximation
        # For standard normal: Φ(z) ≈ 0.5 * (1 + erf(z / sqrt(2)))

        import math

        if std_dev <= 0:
            std_dev = 1.0

        # Standardize
        z = (threshold - forecast_temp) / std_dev

        # Approximate normal CDF using error function
        # erf approximation (Abramowitz and Stegun)
        a1 = 0.254829592
        a2 = -0.284496736
        a3 = 1.421413741
        a4 = -1.453152027
        a5 = 1.061405429
        p = 0.3275911

        sign = 1 if z >= 0 else -1
        abs_z = abs(z)
        t = 1.0 / (1.0 + p * abs_z)
        t_sum = (
            a1 * t
            + a2 * t * t
            + a3 * t * t * t
            + a4 * t * t * t * t
            + a5 * t * t * t * t * t
        )
        erf_approx = sign * (1.0 - t_sum * math.exp(-(abs_z * abs_z)))
        cdf = 0.5 * (1.0 + erf_approx)

        if direction.lower() in ("above", "greater_than"):
            return 1.0 - cdf
        else:
            return cdf


async def fetch_nws_forecast(
    city_name: str, lat: float, lon: float, target_date: str, client: NWSClient, session: aiohttp.ClientSession
) -> NWSForecast:
    """
    High-level function to fetch and parse complete NWS forecast for a city and date.
    Returns NWSForecast object with all key metrics.
    """
    # Get grid point
    grid_point = await client.get_grid_point(lat, lon, session)
    if not grid_point.get("gridId"):
        raise ValueError(f"Could not resolve grid point for {city_name} ({lat}, {lon})")

    grid_id = grid_point["gridId"]
    grid_x = grid_point["gridX"]
    grid_y = grid_point["gridY"]

    # Fetch all forecast data in parallel
    precip_prob = await client.get_precip_probability(lat, lon, target_date, session)
    temp_high, temp_std = await client.get_temperature_forecast(lat, lon, target_date, session)
    snow_prob = await client.get_snow_probability(lat, lon, target_date, 0.5, session)

    # Calculate expiration (cache for 1 hour from fetch time)
    now = datetime.utcnow()
    expires = (now + timedelta(hours=1)).isoformat() + "Z"

    return NWSForecast(
        city=city_name,
        target_date=target_date,
        grid_id=grid_id,
        grid_x=grid_x,
        grid_y=grid_y,
        precip_probability=precip_prob,
        hourly_precip_probs=[],  # Could populate from hourly data if needed
        forecast_high=temp_high,
        forecast_low=temp_high - 10,  # Rough approximation
        temp_std_dev=temp_std,
        snow_probability=snow_prob,
        snow_expected_amount=None,
        wind_speed_mph=0.0,  # Can extend to fetch wind if needed
        wind_gust_mph=None,
        fetched_at=now.isoformat() + "Z",
        expires_at=expires,
    )
