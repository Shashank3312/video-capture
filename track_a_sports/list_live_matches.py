"""
Small helper: print currently in-progress matches with their match_id,
so you can pass one straight into watch.py without digging through
raw JSON.

Usage:
    python list_live_matches.py
"""

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_KEY = os.environ.get("RAPIDAPI_KEY")
API_HOST = "cricbuzz-cricket.p.rapidapi.com"
HEADERS = {"X-RapidAPI-Key": API_KEY or "", "X-RapidAPI-Host": API_HOST}


def main():
    if not API_KEY:
        print("Missing RAPIDAPI_KEY in .env - see .env.example.")
        sys.exit(1)

    response = requests.get(f"https://{API_HOST}/matches/v1/live", headers=HEADERS, timeout=15)
    response.raise_for_status()
    data = response.json()

    found = False
    for type_match in data.get("typeMatches", []):
        for series_match in type_match.get("seriesMatches", []):
            for m in series_match.get("seriesAdWrapper", {}).get("matches", []):
                info = m.get("matchInfo", {})
                if info.get("state") != "In Progress":
                    continue
                found = True
                team1 = info.get("team1", {}).get("teamName", "?")
                team2 = info.get("team2", {}).get("teamName", "?")
                print(f"{info['matchId']}  |  {team1} vs {team2}  |  {info.get('status')}")

    if not found:
        print("No matches currently in progress.")


if __name__ == "__main__":
    main()
