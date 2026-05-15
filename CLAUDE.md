# Sentinel — trader-bot

Autonomous sentiment-driven trading bot on Public.com, deployed to AWS Lambda.

## First-time setup

If `.venv/` does not exist, run setup before anything else:

```bash
chmod +x setup.sh && ./setup.sh
```

This installs Python 3.10+ (if needed), creates a venv, installs all dependencies, and copies `.env.example` → `.env`.

After setup, fill in API keys in `.env`, or open `docs/first-time-setup.html` in a browser for the guided wizard.

## Common commands

```bash
# Test sentiment scan locally (no real orders)
source .venv/bin/activate && python3 test_scan.py

# Deploy to AWS Lambda + update EventBridge schedules
./deploy.sh

# Check portfolio positions and P&L
source .venv/bin/activate && python3 public-sentiment-trader/scripts/get_portfolio.py

# Run a full watchlist sentiment scan
source .venv/bin/activate && python3 public-sentiment-trader/scripts/run_sentiment_scan.py
```

## Project layout

```
lambda_function.py      — Lambda entry point
config/settings.py      — thresholds, watchlist, feature flags
core/agent.py           — Claude Sonnet trade decision agent
scheduler/jobs.py       — 5 scan windows, DynamoDB logging
sentiment/scanner.py    — blends 6 sentiment sources
broker/public_client.py — Public.com API client
api/approval_handler.py — HMAC-signed email approval handler
carpet_bagger/          — Kalshi sports prediction strategy
docs/                   — HTML setup wizard + settings editor
```

## Key facts

- Python 3.12, AWS Lambda us-east-2, EventBridge Scheduler (America/New_York)
- PDT guard: account under $25k — never sell same-day buys
- Options level: LEVEL_2 (calls + puts), no spreads on single equities
- Deploy: `./deploy.sh` — builds in /tmp to avoid iCloud interference
