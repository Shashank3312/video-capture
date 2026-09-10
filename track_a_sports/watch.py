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
a multi-hour match from spamming duplicate notifications. Once a
matching alert fires (batting or bowling), the script prints "Task
completed." and exits - the point is a one-shot notification, not a
running log.

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


def fetch_playing_xi(match_id: int) -> dict[str, list[str]] | None:
    """
    Playing-XI names for both teams, keyed by team name, e.g.
    {"England": [...11 names...], "Pakistan": [...11 names...]}.

    Two extra calls (match overview for team IDs, then one squad call
    per team), made once at startup - not worth doing on every poll.
    Returns None if any of these calls fail, so the caller can decide
    whether to skip validation rather than crash.
    """
    try:
        overview = requests.get(f"https://{API_HOST}/mcenter/v1/{match_id}", headers=HEADERS, timeout=15)
        overview.raise_for_status()
        overview_data = overview.json()

        squads: dict[str, list[str]] = {}
        for team_key in ("team1", "team2"):
            team = overview_data.get(team_key, {})
            team_id = team.get("teamid")
            team_name = team.get("teamname", team_key)
            if team_id is None:
                continue

            squad_resp = requests.get(
                f"https://{API_HOST}/mcenter/v1/{match_id}/team/{team_id}", headers=HEADERS, timeout=15
            )
            squad_resp.raise_for_status()
            groups = squad_resp.json().get("player", [])
            playing_xi = next((g for g in groups if g.get("category") == "playing XI"), {})
            squads[team_name] = [p["name"] for p in playing_xi.get("player", [])]

        return squads
    except requests.RequestException as exc:
        print(f"[warn] couldn't fetch playing XI, skipping the not-in-match check: {exc}")
        return None


def current_innings(scorecard: dict) -> dict | None:
    """
    The innings that is genuinely still in progress.

    Naively "the innings with a not-out batsman" is NOT enough: when
    the last wicket falls (all out) or an innings is declared, the
    batsman who was not out at that moment stays permanently marked
    "outdec": "batting" in this API's data - they really were never
    dismissed, but the innings itself is over. Confirmed on a real
    match: an England innings finished at wickets=10 (all out) still
    showed a stranded not-out batsman as "batting".

    So an innings only counts as live if it's neither declared nor
    all out (wickets < 10), on top of having a not-out batsman. We
    also scan from the most recent innings backwards, since a finished
    earlier innings can still pass that check.
    """
    for innings in reversed(scorecard.get("scorecard", [])):
        if innings.get("isdeclared"):
            continue
        if innings.get("wickets", 0) >= 10:
            continue
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
    Infer the current bowler. Returns (name_or_None, updated_balls_snapshot).

    On the first call (previous_bowler_balls is None) there's no delta
    baseline yet - but unlike a mid-innings poll, we can still spot
    someone who is *already* mid-over right now: their "overs" value
    (e.g. "22.3") has a fractional part, while anyone not currently
    bowling shows a whole number. This mirrors current_batsmen(),
    which also alerts immediately if the target is already batting
    when watching starts - bowling should behave the same way instead
    of always requiring a second poll first.

    On later calls, pick whichever bowler's ball count increased the
    MOST since the last poll, not just the first one found to have
    increased at all. If a poll happens to span more than one
    delivery (API lag/caching, or a slow poll interval), more than one
    bowler's figures can tick up in the same window - e.g. the tail
    end of one over plus the start of the next - and picking by
    largest delta is far more likely to land on whoever is actually
    bowling right now than picking by list order.
    """
    bowlers = innings.get("bowler", [])
    snapshot = {b["name"]: b.get("balls", 0) for b in bowlers}

    if previous_bowler_balls is None:
        mid_over = [b["name"] for b in bowlers if "." in str(b.get("overs", ""))]
        return (mid_over[0] if mid_over else None), snapshot

    best_name = None
    best_delta = 0
    for name, balls in snapshot.items():
        delta = balls - previous_bowler_balls.get(name, 0)
        if delta > best_delta:
            best_delta = delta
            best_name = name

    return best_name, snapshot


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
                print("Task completed.")
                return

        bowler_name, previous_bowler_balls = current_bowler(innings, previous_bowler_balls)
        if bowler_name and bowler_name != previous_bowler:
            if player_name in bowler_name.lower():
                print(f"ALERT: {bowler_name} has come on to bowl!")
                print("Task completed.")
                return
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

    squads = fetch_playing_xi(args.match_id)
    if squads is not None:
        target = args.player_name.strip().lower()
        all_names = [name for names in squads.values() for name in names]
        if not any(target in name.lower() for name in all_names):
            team_list = ", ".join(squads.keys()) or "either team"
            print(f"'{args.player_name}' is not in the playing XI for {team_list} in this match.")
            print("Double-check the spelling, or that this is the right match_id.")
            sys.exit(1)

    try:
        watch(args.match_id, args.player_name, args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
