# NEW: Example usage and testing guide for weather_trader module

"""
Example usage patterns for weather_trader module.
Demonstrates NWS API interaction, market parsing, and trading logic.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================================
# Example 1: Fetch NWS Forecast for a City
# ============================================================================

async def example_nws_forecast():
    """Fetch NWS forecast for NYC on a specific date."""
    from weather_trader import fetch_nws_forecast, NWSClient, CITY_COORDS

    nws = NWSClient()
    async with aiohttp.ClientSession() as session:
        # Get forecast for NYC 5 days from now
        target_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
        city = "NYC"
        lat, lon = CITY_COORDS[city]

        forecast = await fetch_nws_forecast(city, lat, lon, target_date, nws, session)

        print(f"\n{'='*60}")
        print(f"NWS Forecast for {city} on {target_date}")
        print(f"{'='*60}")
        print(f"Precip Probability:    {forecast.precip_probability:.0%}")
        print(f"High Temperature:      {forecast.forecast_high:.0f}°F")
        print(f"Low Temperature:       {forecast.forecast_low:.0f}°F")
        print(f"Temp Std Dev:          {forecast.temp_std_dev:.1f}°F")
        print(f"Snow Probability:      {forecast.snow_probability:.0%}")
        print(f"Fetched:               {forecast.fetched_at}")
        print(f"Expires:               {forecast.expires_at}")


# ============================================================================
# Example 2: Parse Kalshi Market Titles
# ============================================================================

async def example_market_parsing():
    """Parse various Kalshi market title formats."""
    from weather_trader import WeatherMarketParser, CITY_COORDS

    parser = WeatherMarketParser(CITY_COORDS)

    test_markets = [
        {
            "title": "Will NYC high temperature exceed 80°F on April 10?",
            "ticker": "WEATHER_NYC_TEMP_80_APR10",
        },
        {
            "title": "Will it rain more than 0.5 inches in Chicago on April 12?",
            "ticker": "WEATHER_CHI_RAIN_05_APR12",
        },
        {
            "title": "Will Boston receive more than 3 inches of snow on April 15?",
            "ticker": "WEATHER_BOS_SNOW_3_APR15",
        },
        {
            "title": "Will Miami high temp be below 70°F on April 8?",
            "ticker": "WEATHER_MIA_TEMP_70_APR8",
        },
        {
            "title": "Will Seattle have more than 1 inch of rain on April 11?",
            "ticker": "WEATHER_SEA_RAIN_1_APR11",
        },
    ]

    print(f"\n{'='*60}")
    print("Market Parsing Examples")
    print(f"{'='*60}")

    for market in test_markets:
        parsed = parser.parse_market(market)
        if parsed:
            print(f"\n{market['title']}")
            print(f"  City:          {parsed['city']}")
            print(f"  Weather Type:  {parsed['weather_type']}")
            print(f"  Threshold:     {parsed['threshold']}")
            print(f"  Direction:     {parsed['direction']}")
            print(f"  Target Date:   {parsed['target_date']}")
        else:
            print(f"\n{market['title']}")
            print(f"  ❌ FAILED TO PARSE")


# ============================================================================
# Example 3: Compute Temperature Exceedance Probability
# ============================================================================

async def example_temp_probability():
    """Compute probability that temperature exceeds/falls below threshold."""
    from weather_trader import NWSClient

    print(f"\n{'='*60}")
    print("Temperature Exceedance Probability")
    print(f"{'='*60}\n")

    # Scenario 1: NWS forecasts high of 75°F with std dev of 8°F
    forecast_temp = 75.0
    std_dev = 8.0
    threshold = 80.0

    prob_above = NWSClient.compute_temp_exceedance_prob(forecast_temp, std_dev, threshold, "above")
    prob_below = NWSClient.compute_temp_exceedance_prob(forecast_temp, std_dev, threshold, "below")

    print(f"Forecast:    {forecast_temp}°F")
    print(f"Std Dev:     {std_dev}°F")
    print(f"Threshold:   {threshold}°F")
    print(f"P(temp > {threshold}°F) = {prob_above:.1%}")
    print(f"P(temp < {threshold}°F) = {prob_below:.1%}")
    print(f"Sum:         {prob_above + prob_below:.1%} (should be 1.0)")

    # Scenario 2: Very confident forecast
    print(f"\n--- Very Confident Forecast ---")
    forecast_temp = 65.0
    std_dev = 3.0
    threshold = 75.0

    prob_above = NWSClient.compute_temp_exceedance_prob(forecast_temp, std_dev, threshold, "above")
    print(f"Forecast:    {forecast_temp}°F (very confident, std={std_dev}°F)")
    print(f"P(temp > {threshold}°F) = {prob_above:.1%} (very low)")


# ============================================================================
# Example 4: Calculate Edge (NWS vs Kalshi Price)
# ============================================================================

async def example_edge_calculation():
    """Calculate trading edge: NWS probability vs Kalshi market price."""
    from weather_trader import fetch_nws_forecast, NWSClient, WeatherMarketParser, CITY_COORDS

    print(f"\n{'='*60}")
    print("Edge Calculation Example")
    print(f"{'='*60}\n")

    # Fetch NWS forecast
    nws = NWSClient()
    parser = WeatherMarketParser(CITY_COORDS)

    async with aiohttp.ClientSession() as session:
        target_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
        city = "NYC"
        lat, lon = CITY_COORDS[city]

        forecast = await fetch_nws_forecast(city, lat, lon, target_date, nws, session)

        # Parse a hypothetical market
        market = {
            "title": f"Will NYC have more than 0.25 inches of rain on {target_date.split('-')[1]}-{target_date.split('-')[2]}?",
            "ticker": "TEST",
        }
        parsed = parser.parse_market(market)

        # Get NWS probability for YES
        nws_prob = parser.get_nws_probability(parsed, forecast)

        # Simulate Kalshi market prices
        kalshi_yes_ask = 0.55  # Market is asking 55 cents for YES
        kalshi_no_ask = 0.50  # Market is asking 50 cents for NO

        # Calculate edges
        edge_yes = nws_prob - kalshi_yes_ask  # Edge if we buy YES
        edge_no = (1.0 - nws_prob) - kalshi_no_ask  # Edge if we buy NO

        print(f"Market:              {market['title']}")
        print(f"Target Date:         {target_date}")
        print(f"NWS Forecast (YES):  {nws_prob:.0%}")
        print(f"\nKalshi Prices:")
        print(f"  YES Ask:           {kalshi_yes_ask:.0%}")
        print(f"  NO Ask:            {kalshi_no_ask:.0%}")
        print(f"\nEdge Analysis:")
        print(f"  If we buy YES:     NWS {nws_prob:.0%} - Ask {kalshi_yes_ask:.0%} = Edge {edge_yes:+.2f}")
        print(f"  If we buy NO:      NWS {1-nws_prob:.0%} - Ask {kalshi_no_ask:.0%} = Edge {edge_no:+.2f}")

        best_side = "YES" if edge_yes > edge_no else "NO"
        best_edge = max(edge_yes, edge_no)

        print(f"\n  Recommended:       BUY {best_side} (edge {best_edge:+.2f})")

        # Check if it qualifies
        MIN_EDGE = 0.10
        if best_edge > MIN_EDGE:
            print(f"  ✓ QUALIFIES        (edge {best_edge:.2f} > MIN {MIN_EDGE:.2f})")
        else:
            print(f"  ✗ DOES NOT QUALIFY (edge {best_edge:.2f} < MIN {MIN_EDGE:.2f})")


# ============================================================================
# Example 5: Position Sizing Based on Edge
# ============================================================================

async def example_position_sizing():
    """Calculate position size based on edge strength."""
    from weather_trader import (
        MIN_POSITION_SIZE,
        MAX_POSITION_PER_MARKET,
        MIN_EDGE,
    )

    print(f"\n{'='*60}")
    print("Position Sizing Example")
    print(f"{'='*60}\n")

    print(f"Configuration:")
    print(f"  Min Position Size:  ${MIN_POSITION_SIZE}")
    print(f"  Max Position Size:  ${MAX_POSITION_PER_MARKET}")
    print(f"  Min Edge:           {MIN_EDGE:.2f}")

    # Test various edge sizes
    edges = [0.08, 0.10, 0.15, 0.20, 0.30]

    print(f"\nPosition Sizing by Edge:")
    print(f"{'Edge':<10} {'Position Size':<20} {'Qualifies?':<15}")
    print("-" * 45)

    for edge in edges:
        if edge < MIN_EDGE:
            size = 0.0
            qualifies = "No"
        else:
            # Scale position size with edge
            edge_factor = min(1.0, (edge - MIN_EDGE) / 0.20)  # Scale up to 0.30 edge
            size = MIN_POSITION_SIZE + (MAX_POSITION_PER_MARKET - MIN_POSITION_SIZE) * edge_factor
            qualifies = "Yes"

        print(f"{edge:+.2f}      ${size:>6.2f}            {qualifies:<15}")


# ============================================================================
# Example 6: Multi-City Scan
# ============================================================================

async def example_multicity_scan():
    """Scan multiple cities for weather data simultaneously."""
    from weather_trader import fetch_nws_forecast, NWSClient, CITY_COORDS

    print(f"\n{'='*60}")
    print("Multi-City NWS Scan")
    print(f"{'='*60}\n")

    nws = NWSClient()
    target_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")

    async with aiohttp.ClientSession() as session:
        # Fetch forecasts for multiple cities in parallel
        cities_to_scan = ["NYC", "Chicago", "Los Angeles", "Miami", "Seattle"]
        tasks = []

        for city in cities_to_scan:
            if city in CITY_COORDS:
                lat, lon = CITY_COORDS[city]
                task = fetch_nws_forecast(city, lat, lon, target_date, nws, session)
                tasks.append((city, task))

        # Execute all requests in parallel
        results = []
        for city, task in tasks:
            try:
                forecast = await task
                results.append((city, forecast))
            except Exception as e:
                logger.error(f"Failed to fetch {city}: {e}")

        # Display results
        print(f"Forecasts for {target_date}:\n")
        print(f"{'City':<15} {'Precip%':<12} {'High Temp':<12} {'Snow%':<10}")
        print("-" * 50)

        for city, forecast in sorted(results, key=lambda x: x[0]):
            precip_pct = forecast.precip_probability * 100
            temp = forecast.forecast_high
            snow_pct = forecast.snow_probability * 100
            print(f"{city:<15} {precip_pct:>6.0f}%       {temp:>6.0f}°F       {snow_pct:>6.0f}%")


# ============================================================================
# Main: Run All Examples
# ============================================================================

async def main():
    """Run all examples."""
    print("\n" + "=" * 70)
    print("WEATHER TRADER MODULE — USAGE EXAMPLES")
    print("=" * 70)

    try:
        await example_nws_forecast()
        await example_market_parsing()
        await example_temp_probability()
        await example_edge_calculation()
        await example_position_sizing()
        await example_multicity_scan()

        print("\n" + "=" * 70)
        print("ALL EXAMPLES COMPLETED SUCCESSFULLY")
        print("=" * 70 + "\n")

    except Exception as e:
        logger.error(f"Example failed: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
