"""
Live cricket matches, for the app to offer as a choice.

track_a_sports/watch.py needs a match id, and no user has one - it's
an internal Cricbuzz number. list_live_matches.py exists to print them
at a terminal; this is the same lookup shaped for the web app, so the
phone can show "India vs Australia" and pass the id along quietly.

Failures here are expected and normal - the RapidAPI subscription is a
free tier that lapses - so they're raised as one clear exception the
API layer can turn into a message a person can act on, rather than a
stack trace.
"""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_HOST = "cricbuzz-cricket.p.rapidapi.com"


class CricketUnavailable(Exception):
    pass


def live_matches() -> list[dict]:
    api_key = os.environ.get("RAPIDAPI_KEY")
    if not api_key:
        raise CricketUnavailable("No RAPIDAPI_KEY in .env - see .env.example.")

    try:
        response = requests.get(
            f"https://{API_HOST}/matches/v1/live",
            headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": API_HOST},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise CricketUnavailable(f"Couldn't reach the cricket API: {exc}") from exc

    if response.status_code == 403:
        # The exact wording the API returns for a lapsed free plan,
        # which is much more useful to see than "403".
        raise CricketUnavailable(
            "Not subscribed to the Cricbuzz API on RapidAPI. Re-subscribe to its "
            "free plan and this starts working again with the same key."
        )
    if response.status_code == 429:
        raise CricketUnavailable("The cricket API's daily free quota is used up - try again tomorrow.")
    if not response.ok:
        raise CricketUnavailable(f"The cricket API returned {response.status_code}.")

    # 204 No Content, with an empty body, is how the API says "no
    # cricket is being played right now". It's a success code, so it
    # reaches here and would break json() with a parse error that
    # sounds like a broken API instead of a quiet afternoon.
    if response.status_code == 204 or not response.text.strip():
        return []

    matches = []
    for type_match in response.json().get("typeMatches", []):
        for series in type_match.get("seriesMatches", []):
            for match in series.get("seriesAdWrapper", {}).get("matches", []):
                info = match.get("matchInfo", {})
                if info.get("state") != "In Progress":
                    continue
                matches.append(
                    {
                        "match_id": str(info.get("matchId")),
                        "teams": f"{info.get('team1', {}).get('teamName', '?')}"
                                 f" vs {info.get('team2', {}).get('teamName', '?')}",
                        "status": info.get("status", ""),
                        "series": series.get("seriesAdWrapper", {}).get("seriesName", ""),
                    }
                )
    return matches
