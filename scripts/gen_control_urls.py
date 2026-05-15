#!/usr/bin/env python3
"""
Generate permanent bookmark URLs for:
  - Kill switch (pause / resume trading)
  - Settings panel

These URLs contain static HMAC tokens — no expiry.
Bookmark them on your phone. Keep them private.

Usage:
    source .venv/bin/activate
    python3 scripts/gen_control_urls.py
"""
import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from config.settings import settings

secret = settings.SUGGESTION_TOKEN_SECRET
base   = settings.LAMBDA_FUNCTION_URL.rstrip("/")

if not secret:
    print("ERROR: SUGGESTION_TOKEN_SECRET not set in .env")
    sys.exit(1)
if not base:
    print("ERROR: LAMBDA_FUNCTION_URL not set in .env")
    sys.exit(1)


def _token(payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


pause_url    = f"{base}/killswitch?action=pause&token={_token('killswitch:pause')}"
resume_url   = f"{base}/killswitch?action=resume&token={_token('killswitch:resume')}"
settings_url = f"{base}/settings?token={_token('settings:page')}"

print()
print("=" * 70)
print("  TraderBot Control URLs — bookmark these on your phone")
print("  Keep private — anyone with these URLs can control your bot")
print("=" * 70)
print()
print("🛑  PAUSE TRADING:")
print(f"    {pause_url}")
print()
print("✅  RESUME TRADING:")
print(f"    {resume_url}")
print()
print("⚙️   SETTINGS PANEL:")
print(f"    {settings_url}")
print()
