"""
Phase 0, step A: look at what the Cricbuzz Cricket API (via RapidAPI)
actually returns.

CricketData.org (our first pick) turned out not to expose current
batsman/bowler at all on its free tier. pycricbuzz (a free, no-key
wrapper around Cricbuzz's own app backend) turned out to be dead -
its backend host no longer resolves. This is the third and current
candidate: an actively maintained, RapidAPI-hosted mirror of Cricbuzz
data.

We still don't know its exact field names for "who's currently
batting/bowling" or for playing-XI/squads, so - same approach as
before - this script fetches real live data and prints it raw so we
read the real shape once, before writing the matching script
(watch.py) against real field names instead of assumed ones.

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

API_KEY = os.environ.get("RAPIDAPI_KEY")
API_HOST = "cricbuzz-cricket.p.rapidapi.com"
BASE_URL = f"https://{API_HOST}"
HEADERS = {
    "X-RapidAPI-Key": API_KEY or "",
    "X-RapidAPI-Host": API_HOST,
}


def get(path: str):
    response = requests.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=15)
    response.raise_for_status()
    return response.json()


def main():
    if not API_KEY:
        print(
            "Missing RAPIDAPI_KEY. Copy .env.example to .env (in the "
            "project root) and paste your RapidAPI key into it, then run "
            "this again."
        )
        sys.exit(1)

    print("Fetching /matches/v1/live ...\n")
    live = get("/matches/v1/live")
    print(json.dumps(live, indent=2)[:4000])

    # The response nests matches under typeMatches -> seriesMatches ->
    # seriesAdWrapper -> matches. Walk it defensively since we haven't
    # confirmed this shape from docs - only from this live call.
    match_id = None
    match_desc = None
    for type_match in live.get("typeMatches", []):
        for series_match in type_match.get("seriesMatches", []):
            wrapper = series_match.get("seriesAdWrapper", {})
            for m in wrapper.get("matches", []):
                info = m.get("matchInfo", {})
                if info.get("state") in ("In Progress", "Live"):
                    match_id = info.get("matchId")
                    match_desc = info.get("matchDesc")
                    break
            if match_id:
                break
        if match_id:
            break

    if not match_id:
        print("\nCouldn't find a clearly in-progress match in that response.")
        print("Full response was printed above (possibly truncated) -")
        print("inspect it manually to find a matchId to test with.")
        return

    print(f"\nFound a live match: {match_desc} (id={match_id})")
    print(f"\nFetching /mcenter/v1/{match_id}/scard ...\n")
    scorecard = get(f"/mcenter/v1/{match_id}/scard")
    print(json.dumps(scorecard, indent=2)[:6000])


if __name__ == "__main__":
    main()
