"""
Phase 0, step A: look at what CricketData.org's API actually returns.

We don't yet know the exact field names for "who is currently batting/
bowling" - rather than guess, this script fetches real live data and
prints it so we can read the real shape once, then write the matching
script (watch.py) against real field names instead of assumed ones.

Usage:
    python explore_api.py
"""

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_KEY = os.environ.get("CRICAPI_KEY")
BASE_URL = "https://api.cricapi.com/v1"


def get(endpoint: str, **params):
    params["apikey"] = API_KEY
    response = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def main():
    if not API_KEY:
        print(
            "Missing CRICAPI_KEY. Copy .env.example to .env (in the project "
            "root) and paste your free key from https://cricketdata.org/ "
            "into it, then run this again."
        )
        sys.exit(1)

    print("Fetching currentMatches...\n")
    current = get("currentMatches", offset=0)
    print(json.dumps(current, indent=2)[:3000])

    matches = current.get("data", [])
    live_match = next((m for m in matches if m.get("matchStarted") and not m.get("matchEnded")), None)

    if not live_match:
        print("\nNo live match found right now (matchStarted=True, matchEnded=False).")
        print("Try again while a real match is in progress.")
        return

    match_id = live_match["id"]
    print(f"\nFound a live match: {live_match.get('name')} (id={match_id})")
    print("\nFetching match_info for that match...\n")
    info = get("match_info", id=match_id)
    print(json.dumps(info, indent=2)[:5000])


if __name__ == "__main__":
    main()
