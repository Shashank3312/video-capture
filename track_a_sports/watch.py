"""
Phase 0: Track A prototype.

Poll a live cricket match's scorecard and alert when a chosen player
is currently batting or bowling.

Data source: the Cricbuzz Cricket API via RapidAPI (see .env.example
for how to get a key). Field meanings were confirmed by inspecting
real live responses (see explore_api.py), not from official docs:
  - A batsman still at the crease has "outdec" == "batting".
  - There is no explicit "current bowler" flag. We infer it: whichever
    bowler's over/ball count increased since the previous poll is the
    one currently bowling. This is a deliberate inference, not a
    documented guarantee - it can misfire around session breaks or if
    the API's own data lags.

Debounce approach: alert once on the *transition* into
batting/bowling (edge-detection against the previous poll's state),
not on every poll while they're still out there. This is what keeps
a multi-hour match from spamming duplicate notifications.

Usage:
    python watch.py <match_id> "<player name>" [--interval SECONDS]

Find a match_id by running explore_api.py first, or by browsing
Cricbuzz and taking the number from the match URL.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_KEY = os.environ.get("RAPIDAPI_KEY")
API_HOST = "cricbuzz-cricket.p.rapidapi.com"
HEADERS = {"X-RapidAPI-Key": API_KEY or "", "X-RapidAPI-Host": API_HOST}


def fetch_scorecard(match_id: int) -> dict | None:
    url = f"https://{API_HOST}/mcenter/v1/{match_id}/scard"
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        print(f"[warn] couldn't fetch scorecard: {exc}")
        return None


def current_innings(scorecard: dict) -> dict | None:
    """The innings that still has at least one not-out batsman."""
    for innings in scorecard.get("scorecard", []):
        if any(b.get("outdec") == "batting" for b in innings.get("batsman", [])):
            return innings
    return None


def current_batsmen(innings: dict) -> set[str]:
    return {
        b["name"]
        for b in innings.get("batsman", [])
        if b.get("outdec") == "batting"
    }


def current_bowler(innings: dict, previous_bowler_balls: dict[str, int] | None) -> tuple[str | None, dict[str, int]]:
    """
    Infer the current bowler: whoever's ball count went up since the
    last poll. Returns (name_or_None, updated_balls_snapshot).

    On the first call (previous_bowler_balls is None) every bowler's
    ball count would look "increased" from a blank baseline, which
    would misidentify whoever happens to be first in the list - so
    the first call only seeds the baseline and reports no bowler yet.
    """
    snapshot = {b["name"]: b.get("balls", 0) for b in innings.get("bowler", [])}

    if previous_bowler_balls is None:
        return None, snapshot

    bowling_now = None
    for name, balls in snapshot.items():
        if balls > previous_bowler_balls.get(name, -1):
            bowling_now = name
            break

    return bowling_now, snapshot


def watch(match_id: int, player_name: str, interval: int):
    player_name = player_name.strip().lower()
    previously_batting: set[str] = set()
    previous_bowler: str | None = None
    previous_bowler_balls: dict[str, int] | None = None

    print(f"Watching match {match_id} for '{player_name}' (polling every {interval}s)...")

    while True:
        scorecard = fetch_scorecard(match_id)
        if scorecard is None:
            time.sleep(interval)
            continue

        innings = current_innings(scorecard)
        if innings is None:
            print("[info] no innings currently in progress (between innings, or match not live)")
            time.sleep(interval)
            continue

        batting_now = current_batsmen(innings)
        newly_batting = batting_now - previously_batting
        for name in newly_batting:
            if player_name in name.lower():
                print(f"ALERT: {name} has come in to bat!")

        bowler_name, previous_bowler_balls = current_bowler(innings, previous_bowler_balls)
        if bowler_name and bowler_name != previous_bowler:
            if player_name in bowler_name.lower():
                print(f"ALERT: {bowler_name} has come on to bowl!")
            previous_bowler = bowler_name

        previously_batting = batting_now
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Phase 0 - Track A sports scoreboard watcher")
    parser.add_argument("match_id", type=int)
    parser.add_argument("player_name")
    parser.add_argument("--interval", type=int, default=60, help="seconds between polls (default 60)")
    args = parser.parse_args()

    if not API_KEY:
        print(
            "Missing RAPIDAPI_KEY. Copy .env.example to .env (in the "
            "project root) and paste your RapidAPI key into it, then run "
            "this again."
        )
        sys.exit(1)

    try:
        watch(args.match_id, args.player_name, args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
